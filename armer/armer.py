#!/usr/bin/env python3
"""The Armer Class/Driver

This class handles the loading and setup of the ARMer class based on user specified details
"""

from __future__ import annotations

__author__ = ['Gavin Suddrey', 'Dasun Gunasinghe']
__version__ = "0.1.0"

import importlib
import tf2_ros
import yaml
import roboticstoolbox as rtb
import spatialmath as sm
import timeit

from sys import stderr
from typing import List, Dict, Any, Tuple
from armer.utils import populate_transform_stamped
from armer.robots import ROS2Robot
# Add cython global checker
from armer.cython import collision_handler

class Armer:
    """
    The Armer Driver.

    :param robot: [description], List of robots to be managed by the driver
    :type robots: List[rtb.robot.Robot], optional
    :param backend: [description], defaults to None
    :type backend: rtb.backends.Connector, optional
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(
            self,
            node=None,
            robots: List[rtb.robot.Robot] = None,
            backend: rtb.backends.Connector = None,
            backend_args: Dict[str, Any] = None,
            readonly_backends: List[Tuple[rtb.backends.Connector, Dict[str, Any]]] = None,
            publish_transforms: bool = False,
            logging: dict[str, bool] = None) -> None:

        self.robots: List[ROS2Robot] = robots
        self.backend: rtb.backends.Connector = backend
        self.readonly_backends : List[rtb.backends.Connector] = readonly_backends \
            if readonly_backends else []

        if not self.robots:
            if self.node == None:
              print("Using ROS2 Robots but no ROS2 Node provided, exiting", file=stderr)
              return
            self.robots = [ROS2Robot(self, node, rtb.models.URDF.Panda())]

        if not self.backend:
            self.backend = rtb.backends.swift.Swift()

        self.is_publishing_transforms = publish_transforms

        self.broadcaster: tf2_ros.TransformBroadcaster = None

        if self.is_publishing_transforms:
            try:
              self.broadcaster = tf2_ros.TransformBroadcaster(node)
            except TypeError:
              self.broadcaster = tf2_ros.TransformBroadcaster()

        self.frequency = min([r.frequency for r in self.robots])

        # Launch backend
        self.backend.launch(**(backend_args if backend_args else dict()))
        
        # This is a global dictionary of dictionaries (per robot) for multi robot scenarios
        self.global_collision_dict = dict()
        for robot in self.robots:
            # Add robot to backend (with low alpha for collision shapes)
            self.backend.add(robot, collision_alpha=0.2)

            # Resolve robot collision overlaps
            robot.characterise_collision_overlaps()

            # This method extracts all captured collision objects (dict) from each robot
            # Needed to conduct global collision checking (if more than one robot instance is in play)
            # NOTE: all robot instances read the same robot description param, so all robots will get the same description
            #       this may not be the preferred implementation for future use cases.
            self.global_collision_dict[robot.name] = robot.get_link_collision_dict()
        
        # Handle any hardware required initialisation if available
        if hasattr(self.backend, 'hw_initialise'):
            self.backend.hw_initialise()
        
        for readonly, args in self.readonly_backends:
            readonly.launch(**args)

            for robot in self.robots:
                readonly.add(robot, readonly=True)

        # Logging
        self.log_frequency = logging and 'frequency' in logging and logging['frequency']

    def close(self):
        """Close backend and stop action servers"""
        self.backend.close()

        for robot in self.robots:
            robot.close()

    def publish_transforms(self, timestamp) -> None:
        """Publishes transforms (ROS2)"""
        if not self.is_publishing_transforms:
            return

        transforms = []

        for robot in self.robots:
            joint_positions = sm.base.argcheck.getvector(robot.q, robot.n)

            for link in robot.links:
                if link.parent is None:
                    continue

                if link.isjoint:
                    transform = link._Ts @ link._ets[-1].A(joint_positions[link.jindex])
                else:
                    transform = link._Ts

                transforms.append(populate_transform_stamped(
                    link.parent.name,
                    link.name,
                    transform,
                    timestamp
                ))

            for gripper in robot.grippers:
                joint_positions = sm.base.argcheck.getvector(gripper.q, gripper.n)

                for link in gripper.links:
                    if link.parent is None:
                        continue

                    if link.isjoint:
                        transform = link._Ts @ link._ets[-1].A(joint_positions[link.jindex])
                    else:
                        transform = link._Ts

                    transforms.append(populate_transform_stamped(
                        link.parent.name,
                        link.name,
                        transform,
                        timestamp
                    ))
        
        self.broadcaster.sendTransform(transforms)

    @staticmethod
    def load(node, path: str) -> Armer:
        """Generates an Armer Driver instance from the configuration file at path

        :param path: The path to the configuration file
        :type path: str
        :return: An Armer driver instance
        :rtype: Armer
        """
        with open(path, 'r') as handle:
            config = yaml.load(handle, Loader=yaml.SafeLoader)

        robots: List[rtb.robot.Robot] = []

        for spec in config['robots']:
            wrapper = ROS2Robot

            if 'type' in spec:
                module_name, model_name = spec['type'].rsplit('.', maxsplit=1)
                wrapper = getattr(importlib.import_module(module_name), model_name)
                del spec['type']

            if 'model' in spec:
              spec.update(spec['model'])

            urdf_string = None
            if wrapper == ROS2Robot and 'urdf_file' not in spec:
                urdf_param_name = spec['name'] + '/robot_description'
                node.declare_parameter(name=urdf_param_name, value="")
                urdf_string = node.get_parameter(urdf_param_name).get_parameter_value().string_value

            robots.append(wrapper(node, urdf_string=urdf_string, **spec))

        backend = None
        backend_args = dict()

        if 'backend' in config:
            module_name, model_name = config['backend']['type'].rsplit('.', maxsplit=1)
            backend_cls = getattr(importlib.import_module(module_name), model_name)

            backend = backend_cls()
            backend_args = config['args'] if 'args' in config else dict()

        readonly_backends = []

        if 'readonly_backends' in config:
            for spec in config['readonly_backends']:
                module_name, model_name = spec['type'].rsplit('.', maxsplit=1)
                backend_cls = getattr(importlib.import_module(module_name), model_name)

                readonly_backends.append((backend_cls(), spec['args'] if 'args' in spec else dict()))

        logging = config['logging'] if 'logging' in config else {}
        publish_transforms = config['publish_transforms'] if 'publish_transforms' in config else False
        
        return Armer(
            node=node,
            robots=robots,
            backend=backend,
            backend_args=backend_args,
            readonly_backends=readonly_backends,
            publish_transforms=publish_transforms,
            logging=logging
        )
    
    def global_collision_check(self, robot: ROS2Robot):
        """
        Conducts a full check for collisions
        NOTE: takes a given robot object and runs its collision check (of its own dictionary) against the global dictionary
                the global dictionary may have collision data from multiple robots (with different link data)
        TODO: currently each robot is checked against its own link data. This is needed for self collision checking
            but could be possibly optimised in some way as to not be overloaded with multiple instances
        NOTE: [2023-10-31] Identified that this component is very inefficient for the panda (real test). Implemented 
                a start and stop link (e.g., terminating search from end-effector to panda_link8, rather than full tree)
        """
        # Error handling on gripper name
        if robot.gripper == None or robot.gripper == "":
            robot.log(f"Global Collision Check -> gripper name is invalid: {robot.gripper}", 'error')
            return False
        
        # Error handling on empty lick dictionary (should never happen but just in case)
        if robot.link_dict == dict() or robot.link_dict == None:
            robot.log(f"Global Collision Check -> link dictionary is invalid: {robot.link_dict}", 'error')
            return False

        # Error handling on collision object dict and overlap dict
        if robot.overlapped_link_dict == dict() or robot.overlapped_link_dict == None or \
            robot.collision_dict == dict() or robot.collision_dict == None:
            robot.log(f"Global Collision Check -> collision or overlap dictionaries invalid: [{robot.collision_dict}] | [{robot.overlapped_link_dict}]", 'error')
            return False
        
        if robot.collision_sliced_links == None:
            robot.log(f"Global Collision Check -> could not get collision sliced links: [{robot.collision_sliced_links}]", 'error')
            return False

        # Debugging
        # print(f"sliced links: {[link.name for link in robot.collision_sliced_links]}")
        # print(f"col dict -> robots to check: {[robot for robot in self.global_collision_dict.keys()]}")
        # print(f"col dict -> links to check as a dict: {[link for link in self.global_collision_dict.values()]}")
        # print(f"panda_link5 check: {self.global_collision_dict['arm']['panda_link5']}")

        start = timeit.default_timer()
        # Creates a KD tree based on link locations (cartesian translation) to target link (sliced) and extracts 
        # closest links to evaluate (based on dim; e.g., if dim=3, then the 3 closest links will be checked for that link)
        # NOTE: this has yielded a notable improvement in execution without this method (approx. 120%). However, 
        #       it is important to note that the link distances used in the tree are based on that link's origin point.
        #       therefore, there may be cases where the size of the link (physically) is larger than its origin, meaning we miss it in eval
        #       This is something to investigate to robustify this method.
        check_links = robot.query_kd_nn_collision_tree(
            sliced_links=robot.collision_sliced_links, 
            dim=4,
            debug=False
        )
        end = timeit.default_timer()
        # print(f"[KD Setup] full collision check: {1/(end-start)} hz")
        # print(f"[Check Links] -> {check_links}")

        # Alternative Method
        # NOTE: this has between 1-6% increase in speed of execution
        start = timeit.default_timer()
        col_link_id = collision_handler.global_check(
            robot_name = robot.name,
            robot_names = list(self.global_collision_dict.keys()),
            len_robots = len(self.global_collision_dict.keys()),
            robot_links = robot.collision_sliced_links,
            len_links = len(robot.collision_sliced_links),
            global_dict = self.global_collision_dict,
            overlap_dict = robot.overlapped_link_dict,
            check_links = check_links
        )
        end = timeit.default_timer()
        # print(f"[Actual Link Check] full collision check: {1/(end-start)} hz")
    
        if col_link_id >= 0:
            # rospy.logwarn(f"Global Collision Check -> Robot [{robot.name}] in collision with link {robot.collision_sliced_links[col_link_id].name}")
            return True
        else:
            # No collisions found with no errors identified.
            return False
        
    def update_dynamic_objects(self, robot: ROS2Robot) -> None:
        """
        method to handle the addition and removal of dynamic objects per robot instance
        """
        # Check if the current robot has any objects that need removal
        if robot.dynamic_collision_removal_dict:
            for d_obj_name in list(robot.dynamic_collision_removal_dict.copy().keys()):
                robot.log(f"Removal of Dynamic Objects in Progress", 'warn')
                # remove from backend
                # NOTE: there is a noted bug in the swift backend that sets the object 
                #       (in a separate dictionary called swift_objects) to None. In the self.backend.step()
                #       method below, this attempts to run some methods that belong to the shape but cannot do so
                #       as it is a NoneType.
                shape_to_remove = robot.dynamic_collision_removal_dict[d_obj_name].shape
                robot.log(f"Remove object is: {shape_to_remove}")
                # TODO: add this feature in once swift side is fixed 
                #       should still work for ROS backend
                # self.backend.remove(shape_to_remove)
                # remove from robot dict
                robot.dynamic_collision_removal_dict.pop(d_obj_name)
                robot.log(f"Removed successfully")
        else:
            # Check if the current robot has any newly added objects to add to the backend
            # NOTE: this loop is run everytime at the moment (not an issue with limited shapes but needs better optimisation for scale)
            for dynamic_obj in robot.dynamic_collision_dict.copy().values():
                if dynamic_obj.is_added == False:
                    robot.log(f"Adding Dynamic Object: {dynamic_obj}")
                    dynamic_obj.id = self.backend.add(dynamic_obj.shape)
                    dynamic_obj.is_added = True
                    robot.log(f"Added Successfully")

    def step(self, dt: float, current_time: float) -> None:
        """Main step method - controlled by ROS2 node"""
        for robot in self.robots:
            if self.global_collision_check(robot=robot) and robot.preempted == False:
                # Current robot found to be in collision so preempt
                robot.collision_approached = True
                robot.preempt()

            if robot.preempted == False:
                # Set the safe state of robot for recovery on collisions if needed
                robot.set_safe_state()

            # Check if the current robot has any  dynamic objects that need backend update
            self.update_dynamic_objects(robot=robot)
                
            robot.step(dt=dt)

        # with Timer('step'):
        self.backend.step(dt=dt)

        for backend, args in self.readonly_backends:
            backend.step(dt=dt)

        self.publish_transforms(current_time)