#!/usr/bin/env python
#
# Copyright 2009 Mike Wakerly <opensource@hoho.com>
#
# This file is part of the Pykeg package of the Kegbot project.
# For more information on Pykeg or Kegbot, see http://kegbot.org/
#
# Pykeg is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# Pykeg is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Pykeg.  If not, see <http://www.gnu.org/licenses/>.

"""Kegboard daemon.

The kegboard daemon is the primary interface between a kegboard devices and a
kegbot system.  The process is responsible for several tasks, including:
  - discovering kegboards available locally
  - connecting to the kegbot core and registering the individual boards
  - accumulating data if the kegbot core is offline

The kegboard daemon is compatible with any device that speaks the Kegboard
Serial Protocol. See http://kegbot.org/docs for the complete specification.

The daemon should run on any machine which is attached to kegboard hardware.

The daemon must connect to a Kegbot Core in order to publish data (such as flow
and temperature events).  This is a TCP connection, using the Kegnet Protocol to
exchange data.
"""

import asynchat
import asyncore
import cStringIO
import datetime
import logging
import os
import Queue
import socket
import sys

import serial

import importhacks
from pykeg.core import kb_app
from pykeg.core import kb_common
from pykeg.core import util
from pykeg.core.net import kegnet_client
from pykeg.hw.kegboard import kegboard
from pykeg.external.gflags import gflags

FLAGS = gflags.FLAGS

gflags.DEFINE_integer('cache_seconds', 60,
    'If nonzero, the kegnet client will suppress duplicate sensor '
    'readings for this many seconds.')

class DuplicateSuppressingCache:
  def __init__(self, max_age=FLAGS.cache_seconds):
    self._max_delta = datetime.timedelta(seconds=max_age)
    self._last_reading_time = {}
    self._last_reading_value = {}

  def ShouldSuppress(self, key, value):
    now = datetime.datetime.now()
    last_time = self._last_reading_time.get(key)
    last_value = self._last_reading_value.get(key)

    if last_time is None:
      self._last_reading_time[key] = now
      self._last_reading_value[key] = value
      return False
    elif value != last_value:
      self._last_reading_time[key] = now
      self._last_reading_value[key] = value
      return False
    else:
      # Values are the same
      if (now - last_time) < self._max_delta:
        return True
      else:
        self._last_reading_time[key] = now
        self._last_reading_value[key] = value
        return False

class KegboardManagerApp(kb_app.App):
  def __init__(self, name='core', daemon=FLAGS.daemon):
    kb_app.App.__init__(self, name, daemon)

  def _Setup(self):
    kb_app.App._Setup(self)

    self._client = kegnet_client.KegnetClient()

    self._manager_thr = KegboardManagerThread('kegboard-manager',
        self._client)
    self._AddAppThread(self._manager_thr)

    self._device_io_thr = KegboardDeviceIoThread('device-io', self._manager_thr,
        FLAGS.kegboard_device, FLAGS.kegboard_speed)
    self._AddAppThread(self._device_io_thr)

class KegboardManagerThread(util.KegbotThread):
  """Manager of local kegboard devices."""

  def __init__(self, name, client):
    util.KegbotThread.__init__(self, name)
    self._client = client
    self._message_queue = Queue.Queue()
    self._cache = DuplicateSuppressingCache()

  def PostDeviceMessage(self, device_name, device_message):
    """Receive a message from a device, for processing."""
    self._message_queue.put((device_name, device_message))

  def run(self):
    self._logger.info('Starting main loop.')
    while not self._quit:
      try:
        device_name, device_message = self._message_queue.get(timeout=1.0)
      except Queue.Empty:
        continue

      try:
        self._HandleDeviceMessage(device_name, device_message)
      except kegnet_client.ClientException, e:
        self._logger.warning('Got error during send: %s' % (e,))

    self._logger.info('Exiting main loop.')

  def _HandleDeviceMessage(self, device_name, msg):
    if isinstance(msg, kegboard.MeterStatusMessage):
      # Flow update: compare to last value and send a message if needed
      meter_name = msg.meter_name.GetValue()
      curr_val = msg.meter_reading.GetValue()
      suppress = self._cache.ShouldSuppress(('meter', meter_name), curr_val)

      if not suppress:
        self._client.SendMeterUpdate(meter_name, curr_val)

    elif isinstance(msg, kegboard.TemperatureReadingMessage):
      sensor_name = msg.sensor_name.GetValue()
      sensor_value = msg.sensor_reading.GetValue()
      suppress = self._cache.ShouldSuppress(('thermo', sensor_name), sensor_value)

      if not suppress:
        self._client.SendThermoUpdate(sensor_name, sensor_value)


class KegboardDeviceIoThread(util.KegbotThread):
  """Manages all device I/O.

  This thread continuously reads from attached kegboard devices and passes
  messages to the KegboardManagerThread.
  """
  def __init__(self, name, manager, device_path, device_speed):
    util.KegbotThread.__init__(self, name)
    self._manager = manager
    self._device_path = device_path
    self._device_speed = device_speed

    self._reader = None
    self._serial_fd = None

  def _SetupSerial(self):
    self._logger.info('Setting up serial port...')
    self._serial_fd = serial.Serial(self._device_path, self._device_speed)
    self._reader = kegboard.KegboardReader(self._serial_fd)

  def run(self):
    self._SetupSerial()
    try:
      self._MainLoop()
    finally:
      self._serial_fd.close()

  def _MainLoop(self):
    self._logger.info('Starting reader loop...')
    while not self._quit:
      msg = self._reader.GetNextMessage()
      self._manager.PostDeviceMessage('kegboard', msg)
    self._logger.info('Reader loop ended.')


class KegboardNetworkThread(util.KegbotThread):
  """ Object that connects a kegboard stream to a KegnetProtocolClient. """
  def __init__(self, name, device_name, client):
    util.KegbotThread.__init__(self, name)
    self._device_name = device_name
    self._sock_map = {}
    self._client = client

  def GetClient(self, device_name):
    return self._client

  def run(self):
    self._logger.info("Running asyncore loop.")
    asyncore.loop(map=self._sock_map)


if __name__ == '__main__':
  KegboardManagerApp.BuildAndRun()