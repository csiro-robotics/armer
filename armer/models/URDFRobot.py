#!/usr/bin/env python3
"""URDFRobot module defines the URDFRobot type

Defines the  robot based on URDF read (parameter) or file 

"""

from __future__ import annotations

__author__ = ['Gavin Suddrey', 'Dasun Gunasinghe']
__version__ = "0.2.0"

import numpy as np
import re
import time
import xml.etree.ElementTree as ETT
import spatialmath as sm

from ament_index_python.packages import get_package_share_directory, PackageNotFoundError

from sys import stderr
from io import BytesIO
from roboticstoolbox.robot import Robot, Link, ET, ETS
from roboticstoolbox.tools import URDF


class URDFRobot(Robot):
  def __init__(self,
               qz=None,
               qr=None,
               gripper=None,
               collision_check_start_link=None,
               collision_check_stop_link=None,
               tool=None,
               urdf_string=None,
               urdf_file=None,
               logger=None,
               *args,
               **kwargs):
    

    # Default class validity
    self.valid = False
    
    # Optional logger
    if logger:
      self.logger = logger

    # Read URDF if specified as a file or from the ROS parameter server
    if urdf_file:
      self.log(f"urdf_file: {urdf_file}")
      links, name, urdf_string, urdf_filepath = self.URDF_read(urdf_file)
    elif urdf_string:
      links, name, urdf_string, urdf_filepath = URDFRobot.URDF_read_string(urdf_string)
    else:
      self.log(f"No URDF Given", mode='error')
      return

    # Error handling on URDF read failure
    if links == None:
      self.log(f"Links not configured. Exiting.", mode='error')
      return

    # Define/configure the gripper based on URDF read links
    self.gripper = gripper if gripper else URDFRobot.resolve_gripper(links)
    gripper_link = list(filter(lambda link: link.name == self.gripper, links))
    
    # Configure a tool if specified
    if tool:
      ets = URDFRobot.resolve_ets(tool)
      
      if 'name' in tool:
        links.append(Link(ets, name=tool['name'], parent=self.gripper))
        self.gripper = tool['name']

      gripper_link[0].tool = sm.SE3(ets.compile()[0].A())
      
    # Handle collision stopping link if invalid
    link_names = [link.name for link in links]
    if not collision_check_start_link or collision_check_start_link not in link_names:
      self.collision_check_start_link = self.gripper
      self.log(f"Invalid collision start link {collision_check_start_link} -> defaulting to gripper: {self.collision_check_start_link}", mode='warn')
    else:
      self.collision_check_start_link = collision_check_start_link

    if not collision_check_stop_link or collision_check_stop_link not in link_names:
      self.collision_check_stop_link = links[0].name
      self.log(f"Invalid collision stop link {collision_check_stop_link} -> defaulting to base: {self.collision_check_stop_link}", mode='warn')
    else:
      self.collision_check_stop_link = collision_check_stop_link
    
    self.log(f"Collision Link Window on Initialisation: {self.collision_check_start_link} to {self.collision_check_stop_link}")
    
    # Set validity as successful
    self.valid = True
    
    super().__init__(
        arg=links,
        name=name,
        gripper_links=gripper_link,
        urdf_string=urdf_string,
        urdf_filepath=urdf_filepath,
    )

    self.qr = qr if qr else np.array([0] * self.n)
    self.qz = qz if qz else np.array([0] * self.n)
    self.addconfiguration("qr", self.qr)
    self.addconfiguration("qz", self.qz)

  @staticmethod
  def URDF_read_string(urdf_string):
    """ Read URDF From a provided String, usually parsed by the high-level application from a ROS2 Parameter.
    """

    urdf_string = URDFRobot.URDF_resolve(urdf_string)

    if urdf_string:
      tree = ETT.parse(
        BytesIO(bytes(urdf_string, "utf-8")), 
        parser=ETT.XMLParser()
      )
      node = tree.getroot()
      urdf = URDF._from_xml(node, '/')
      return urdf.elinks, urdf.name, urdf_string, '/'
    else:
      return None, None, None, None
    
  @staticmethod
  def URDF_resolve(urdf_string):
    """ Resolves ROS2 package paths inside a urdf_string using ament_python
    """
    packages = list(set(re.findall(r'(package:\/\/([^\/]*))', urdf_string)))
    for package in packages:
      try:
        urdf_string = urdf_string.replace(package[0], get_package_share_directory(package[1]))
      except PackageNotFoundError:
        urdf_string = None
    return urdf_string

  @staticmethod
  def resolve_gripper(links):
    parents = []
    for link in links:
      if not link.parent:
        continue
      if link.parent.name in parents:
        return link.parent.name
      parents.append(link.parent.name)
    return links[-1].name

  @staticmethod
  def resolve_ets(tool):
    transforms = [ET.tx(0).A()]
    if 'ets' in tool:
      transforms = [ getattr(ET, list(et.keys())[0])(list(et.values())[0]) for et in tool['ets'] ]
    return ETS(transforms)
  
  def log(self, message, mode='info'):
    """ Logging wrapper to allow this class to log to a ROS2 Logger if specified
    """
    if hasattr(self, 'logger'):
      if mode == 'error':
        self.logger.error(message)
      elif mode == 'warn':
        self.logger.warn(message)
      else:
        self.logger.info(message)
    elif mode == 'error' or mode == 'warn':
      print(message, file=stderr)
    else:
      print(message)

  def is_valid(self):
    return self.valid

if __name__ == "__main__":  # pragma nocover

    # TODO: change this to a known, more traceable xacro
    r = URDFRobot(urdf_file='ur_description/urdf/ur5_joint_limited_robot.urdf.xacro')
    print(r)
    