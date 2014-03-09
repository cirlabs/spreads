from __future__ import division

import logging
import re
import subprocess
from contextlib import contextmanager
from datetime import datetime
from functools import wraps

from flask import request, url_for, abort
from jpegtran import JPEGImage
from werkzeug.contrib.cache import SimpleCache
from werkzeug.routing import BaseConverter, ValidationError

from persistence import get_workflow


logger = logging.getLogger('spreadsplug.web.util')

# NOTE: This is a workaround for a known datetime/time race condition, see
#       this CPython bugreport for more details:
#       http://bugs.python.org/issue7980
datetime.strptime("2014", "%Y")


def scale_image(img_name, width=None, height=None):
    if width is None and height is None:
        raise ValueError("Please specify either width or height")
    img = JPEGImage(img_name)
    aspect = img.width/img.height
    width = width if width else int(aspect*height)
    height = height if height else int(width/aspect)
    return img.downscale(width, height).as_blob()


class WorkflowConverter(BaseConverter):
    def to_python(self, value):
        workflow_id = None
        try:
            workflow_id = int(value)
        except ValueError:
            raise ValidationError()
        workflow = get_workflow(workflow_id)
        if workflow is None:
            abort(404)
        return workflow

    def to_url(self, value):
        return unicode(value.id)


# NOTE: The cache object is global
cache = SimpleCache()


def cached(timeout=5 * 60, key='view/%s'):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            cache_key = key % request.path
            rv = cache.get(cache_key)
            if rv is not None:
                return rv
            rv = f(*args, **kwargs)
            cache.set(cache_key, rv, timeout=timeout)
            return rv
        return decorated_function
    return decorator


def workflow_to_dict(workflow):
    out_dict = dict()
    out_dict['id'] = workflow.id
    out_dict['name'] = workflow.path.name
    out_dict['step'] = workflow.step
    out_dict['step_done'] = workflow.step_done
    out_dict['images'] = [get_image_url(workflow, x)
                          for x in workflow.images] if workflow.images else []
    out_dict['out_files'] = ([unicode(path) for path in workflow.out_files]
                             if workflow.out_files else [])
    out_dict['config'] = workflow.config.flatten()
    return out_dict


def get_image_url(workflow, img_path):
    img_num = int(img_path.stem)
    return url_for('.get_workflow_image', workflow=workflow, img_num=img_num)


def parse_log_lines(logfile, from_line, levels=['WARNING', 'ERROR']):
    # "%(asctime)s %(message)s [%(name)s] [%(levelname)s]"))
    # 2014-01-14 22:00:48,734 1 thread(s) still running [waitress] [WARNING]
    msg_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"  # timestamp
        r" (.*?)"  # message
        r" \[(.*?)]"  # logger
        r" \[(\w+)]",  # loglevel
        re.MULTILINE
    )
    with logfile.open('r') as fp:
        loglines = fp.readlines()
    new_lines = loglines[from_line:]
    messages = [{'time': datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S,%f'),
                 'message': message,
                 'origin': origin,
                 'level': loglevel}
                for timestamp, message, origin, loglevel
                in (msg_re.findall(x)[0] for x in new_lines
                    if msg_re.match(x))
                if loglevel in levels]
    messages.sort(key=lambda e: e['time'])
    return messages


def get_thumbnail(img_path):
    """ Return thumbnail for image.

    :param img_path:  Path to image
    :type img_path:   pathlib.Path
    :return:          The thumbnail
    :rtype:           bytestring
    """
    thumb = JPEGImage(unicode(img_path)).exif_thumbnail.as_blob()
    if thumb:
        logger.debug("Using EXIF thumbnail for {0}".format(img_path))
        return thumb
    else:
        logger.debug("Generating thumbnail for {0}".format(img_path))
        return scale_image(unicode(img_path), width=160)


def _find_stick_dbus():
    bus = dbus.SystemBus()
    iudisks = dbus.Interface(
        bus.get_object("org.freedesktop.UDisks", "/org/freedesktop/UDisks"),
        "org.freedesktop.UDisks")
    devices = [bus.get_object("org.freedesktop.UDisks", dev)
               for dev in iudisks.EnumerateDevices()]
    idevices = [dbus.Interface(dev, "org.freedesktop.DBus.Properties")
                for dev in devices]
    try:
        istick = next(i for i in idevices if i.Get("", "DeviceIsPartition")
                      and i.Get("", "DriveConnectionInterface") == "usb")
    except StopIteration:
        return
    return dbus.Interface(
        bus.get_object("org.freedesktop.UDisks",
                       iudisks.FindDeviceByDeviceFile(
                           istick.Get("", "DeviceFile"))),
        "org.freedesktop.DBus.UDisks.Device")


def _find_stick_shell():
    """ Since the 'dbus' module is implemented via CPython's extension API,
    we cannot use it when running in PyPy. The same goes for GOBject
    introspection via PyGobject. 'pgi' is not yet mature enough to deal with
    some of UDisks' interfaces.
    Thats why we go a very hacky way: Use the 'gdbus' CLI utility, parse
    its output and return the device path.
    """
    out = subprocess.check_output(
        "gdbus introspect --system --dest org.freedesktop.UDisks "
        "--object-path /org/freedesktop/UDisks/devices --recurse "
        "--only-properties".split())
    devs = zip(*((re.match(r".* = '?(.*?)'?;", x).group(1)
                  for x in out.splitlines()
                  if "DriveConnectionInterface =" in x
                  or "DeviceIsPartition =" in x
                  or "DeviceFile = " in x),)*3)
    return next(dev[2] for dev in devs[0] == 'usb' and devs[1] == 'true')



@contextmanager
def _mount_stick_dbus(stick):
    """ Context Manager that mounts the first available partition on a USB
    drive, yields its path and then unmounts it.

    """
    mount = stick.get_dbus_method(
        "FilesystemMount", dbus_interface="org.freedesktop.UDisks.Device")
    path = mount('', [])
    try:
        yield path
    except Exception as e:
        raise e
    finally:
        unmount = stick.get_dbus_method(
            "FilesystemUnmount",
            dbus_interface="org.freedesktop.UDisks.Device")
        unmount([], timeout=1e6)  # dbus-python doesn't know an infinite
                                  # timeout... unmounting sometimes takes a
                                  # long time, since the device has to be
                                  # synced.

@contextmanager
def _mount_stick_shell(stick):
    """ Context Manager that mounts the first available partition on a USB
    drive, yields its path and then unmounts it.

    """
    out = subprocess.check_output("udisks --mount {0}".format(stick).split())
    path = re.match(r"Mounted .* at (.*)", out).group(1)
    try:
        yield path
    except Exception as e:
        raise e
    finally:
        subprocess.check_output("udisks --unmount {0}".format(stick).split())

try:
    import dbus
    find_stick = _find_stick_dbus
    mount_stick = _mount_stick_dbus
except ImportError:
    find_stick = _find_stick_shell
    mount_stick = _mount_stick_shell
