#!/usr/bin/env python3
"""The Armer ROS2 Node Class

This class handles the ROS2 function of the ARMer class
"""

from __future__ import annotations

__author__ = ['Gavin Suddrey', 'Dasun Gunasinghe']
__version__ = "0.1.0"

import os
import rclpy
import ament_index_python
import numpy as np
from armer.armer import Armer
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import ReentrantCallbackGroup

__path__ = ament_index_python.packages.get_package_share_directory('armer')

class ArmerNode(Node):
  """Main ARMer Node
  
  The main Node running in ROS2
  """
  def __init__(self):
    super().__init__('armer')

    # NOTE: deprecation notice on just a name being set, requires a default value (other than None)
    #       default set to current path config to panda simulation yaml
    self.declare_parameter(name='config', value=os.path.join(__path__, 'cfg/panda_sim.yaml'))
    
    # NOTE: defaults to the panda simulation 
    #       attempts load from parameter server or current path to panda simulation config (default)
    self.armer = Armer.load(
      self, 
      self.get_parameter_or('config').get_parameter_value().string_value or os.path.join(__path__, 'cfg/panda_sim.yaml')
    )

    # Setup the ROS timer callback to run the main ARMer functionality (step method)
    self.last_time = self.get_clock().now()
    self.timer = self.create_timer(
      timer_period_sec=(1 / self.armer.frequency), 
      callback=self.timer_callback, 
      callback_group=ReentrantCallbackGroup()
    )

  def timer_callback(self):
    """ROS2 Callback Mechanism
    
    Set to configured frequency (init)
    NOTE: 
    """
    current_time = self.get_clock().now()
    # Get dt in seconds (REQUIRED)
    dt = (current_time - self.last_time).nanoseconds / 1e9
    
    # DEBUGGING
    # print(f"dt: {dt} | current time: {current_time} | last_time: {self.last_time}")

    # Step the ARMer class and update configured robot(s)
    # NOTE: dt MUST be in seconds
    self.armer.step(dt, current_time)
    self.last_time = current_time

def main(args=None):
    rclpy.init(args=args)

    try:
      armer = ArmerNode()
      executor = MultiThreadedExecutor(num_threads=4)
      executor.add_node(armer)

      try:
          executor.spin()
      finally:
          executor.shutdown()
          armer.destroy_node()

    finally:
        rclpy.shutdown()

if __name__ == '__main__':
  main()