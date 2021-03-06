#!/usr/bin/env python
#coding=utf8
'''
Headmouse!
'''

import logging

# initial log config to handle load/import time errors
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

import time
import threading
import sys
import os
import ConfigParser
import re

import cv2

import camera
import filters
import util

try:
    import pymouse
except ImportError:
    logger.warn("Unable to load PyMouse. Install PyUserinput for direct mouse control.")

try:
    import arduino_serial
except ImportError:
    # TODO
    pass

GLOBAL_CONFIG_FILE = "/etc/headmouse.conf"
USER_CONFIG_FILE = os.path.expanduser("~/.headmouse")

ACCELERATION_EXPONENT = 2
OUTLIER_VELOCITY_THRESHOLD = 20

def consumer(func):
    '''
    Decorator taking care of initial next() call to "sending" generators

    From PEP-342
    http://www.python.org/dev/peps/pep-0342/
    '''
    def wrapper(*args,**kw):
        gen = func(*args, **kw)
        next(gen)
        return gen
    wrapper.__name__ = func.__name__
    wrapper.__dict__ = func.__dict__
    wrapper.__doc__  = func.__doc__
    return wrapper

## Output drivers

@consumer
def arduino_output(config=None):
    '''Write mouse coordinates out to Arduino via serial'''
    arduino = arduino_serial.get_serial_link(config['arduino_port'], config['arduino_baud'], timeout=1, slices=3)
    while True:
        x, y = yield
        arduino.move_mouse(x, y)

@consumer
def print_output(config=None):
    '''Write mouse coordinate changes out to stdout'''
    while True:
        x, y = yield
        print("{:d}, {:d}".format(x, y))

@consumer
def pymouse_output(config=None):
    '''Write mouse coordinates out to pymouse'''

    try:
        logger.warn("Loading PyMouse; some versions may hang while loading for up to 30 seconds.")
        import pymouse
    except ImportError:
        logger.warn("Unable to load PyMouse. Install PyMouse for direct mouse control.")
    logger.info("Done Loading pymouse")


    mouse = pymouse.PyMouse()
    x_max, y_max = mouse.screen_size()
    while True:
        dx, dy = yield
        x, y = mouse.position()
        x = max(0, min(x_max, x + dx))
        y = max(0, min(y_max, y + dy))
        if x < 0 or x_max < x or y < 0 or y_max < y:
            logger.debug("{}, {}".format(x, y))
        mouse.move(x, y)

def get_config(custom_config_file=None):
    config = {
        'output': 'arduino_output',
        'arduino_baud': 115200,
        'arduino_port': '/dev/tty.usbmodemfa13131',

        'input': 'camera',
        'input_tracker': 'dot_tracker',
        'input_visualize': True,
        'input_realtime_search_timeout': 2.0,
        'input_slow_search_delay': 2.0,

        'input_camera_name': 0,
        'input_camera_resolution': (640, 480),
        'input_camera_fps': 30,

        'acceleration': 2.3,
        'sensitivity': 2.0,
        'smoothing': 0.90,
        'output_smoothing': 0.90,
        'distance_scaling': True,

        'verbosity': 3,
    }

    # parse config files and override hardcoded defaults
    for config_file in\
        (custom_config_file,) if custom_config_file is not None else\
        (GLOBAL_CONFIG_FILE, USER_CONFIG_FILE):
        if os.path.exists(config_file):
            config_parser = ConfigParser.SafeConfigParser()
            config_parser.read([config_file])
            from_file = dict(config_parser.items('headmouse'))
            config.update(from_file)

    # TODO: argparse overrides

    # type hacks... 
    # TODO: something better

    # split resolution like "640x480" or "640, 480" into pair of ints
    if isinstance(config['input_camera_resolution'], basestring):
        config['input_camera_resolution'] = [int(x) for x in re.split(r'x|, *', config['input_camera_resolution'])]

    # int config fields
    for field in (
            'input_camera_name',
            'input_camera_fps', 
            'arduino_baud',
            'verbosity'
        ):
        config[field] = int(config[field])

    # float config fields
    for field in (
            'acceleration', 
            'sensitivity', 
            'smoothing', 
            'output_smoothing', 
            'input_realtime_search_timeout', 
            'input_slow_search_delay'
        ):
        config[field] = float(config[field])

    # bool config fields
    for field in (
            'distance_scaling',
            'input_visualize',
        ):
        if isinstance(config[field], basestring):
            config[field] = config[field].lower() in ("true", "1", "t", "#t", "yes")
        else:
            config[field] = bool(config[field])

    return config

def main():
    '''Headmouse main loop'''
    config = get_config()

    # configure logging manually so we can use runtime config settings
    log_level = [logging.ERROR, logging.WARN, logging.INFO, logging.DEBUG][config['verbosity']]

    # must get root logger, set its level, attach handler, and set its level
    root_logger = logging.getLogger('')
    console = logging.StreamHandler()

    root_logger.setLevel(log_level)
    console.setLevel(log_level)

    root_logger.addHandler(console)

    # output driver setup
    # TODO: restrict loadable generaton functions for security
    # TODO: driver loading system that doesn't depend on __main__ - breaks cProfile
    output_driver = sys.modules[__name__].__dict__[config['output']](config=config)

    # signal proc chain setup
    velocity_gen = filters.relative_movement()
    sub_pix_gen = filters.sub_pix_trunc()
    stateful_smooth_gen = filters.stateful_smoother()
    input_smoother_gen = filters.ema_smoother(config['smoothing'])
    #slow_smoother_gen = filters.slow_smoother(.6)
    acceleration_gen = filters.accelerate_exp(
        p=ACCELERATION_EXPONENT,
        accel=config['acceleration'], 
        sensitivity=config['sensitivity']
        )

    output_smoother = filters.ema_smoother(config['output_smoothing'])

    # input driver setup
    camera.visualize = config['input_visualize']

    fps_stats = util.Stats(util.Stats.inverse_normalized_interval_delta, "Average frame rate {:.0f} fps", 10)
    with camera.camera(
            tracker_name=config['input_tracker'],
            camera_id=config['input_camera_name'],
            resolution=config['input_camera_resolution'],
            fps=config['input_camera_fps'],
            realtime_search_timeout=config['input_realtime_search_timeout'],
            slow_search_delay=config['input_slow_search_delay']
        ) as input_source:
        # main loop
        for x, y, distance in input_source:
            fps_stats.push(time.time())

            # Capture frame-by-frame

            ### Filter Section ###
            # take absolute position return relative position
            v = velocity_gen.send((x, y))
            v = filters.killOutliers(v, OUTLIER_VELOCITY_THRESHOLD)

            if config['distance_scaling']:
                dx, dy = v
                v = dx * distance, dy * distance

            #v = slow_smoother_gen.send((v, 6))
            v = input_smoother_gen.send(v)
            v = acceleration_gen.send(v)
            #v = filters.accelerate(v)

            dx, dy = sub_pix_gen.send(v)

            # mirror motion on x-axis
            dx *= -1

            dx, dy = output_smoother.send((dx, dy))

            output_driver.send((dx,dy))

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    return 0

if __name__ == "__main__":
    sys.exit(main())
