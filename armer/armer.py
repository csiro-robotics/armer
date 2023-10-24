"""
Armer Class

.. codeauthor:: Gavin Suddreys
.. codeauthor:: Dasun Gunasinghe
"""
from __future__ import annotations
from typing import List, Dict, Any, Tuple

import timeit
import importlib

import rospy
import tf2_ros
import yaml

import roboticstoolbox as rtb
import spatialgeometry as sg
import spatialmath as sm
from roboticstoolbox.backends.swift import Swift

from spatialmath.base.argcheck import getvector

from armer.utils import populate_transform_stamped
from armer.models import URDFRobot
from armer.robots import ROSRobot
from armer.timer import Timer


class Armer:
    """
    The Armer Driver.

    :param robot: [description], List of robots to be managed by the driver
    :type robots: List[rtb.robot.Robot], optional
    :param backend: [description], defaults to None
    :type backend: rtb.backends.Connector, optional

    .. codeauthor:: Gavin Suddrey
    .. sectionauthor:: Gavin Suddrey
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(
            self,
            robots: List[rtb.robot.Robot] = None,
            backend: rtb.backends.Connector = None,
            backend_args: Dict[str, Any] = None,
            readonly_backends: List[Tuple[rtb.backends.Connector, Dict[str, Any]]] = None,
            publish_transforms: bool = False,
            logging: dict[str, bool] = None) -> None:

        self.robots: List[ROSRobot] = robots
        self.backend: rtb.backends.Connector = backend
        self.readonly_backends : List[rtb.backends.Connector] = readonly_backends \
            if readonly_backends else []
        self.backend_args = backend_args

        if not self.robots:
            self.robots = [ROSRobot(self, rtb.models.URDF.UR5())]

        if not self.backend:
            self.backend = Swift()

        self.is_publishing_transforms = publish_transforms

        self.broadcaster: tf2_ros.TransformBroadcaster = None

        if self.is_publishing_transforms:
            self.broadcaster = tf2_ros.TransformBroadcaster()

        self.frequency = min([r.frequency for r in self.robots])
        self.rate = rospy.Rate(self.frequency)

        self.last_tick = rospy.get_time()

        # Launch backend
        self.backend.launch(**(backend_args if backend_args else dict()))

        # print(f"init links:")
        for robot in self.robots:
            # Add robot to the backend
            self.backend.add(robot, collision_alpha=0.2)
            
            # Resolve robot links for collision checking
            # NOTE: must be done after adding to backend
            # TODO: confirm with ROS backend
            robot.resolve_collision_tree()


            # # TESTING
            # # Add dummy object for testing
            # s0 = sg.Sphere(radius=0.05, pose=sm.SE3(0.5, 0, 0.5))
            # s1 = sg.Sphere(radius=0.05, pose=sm.SE3(0.5, 0, 0.1))
            # robot.add_collision_obj(s0)
            # robot.add_collision_obj(s1)
            # self.backend.add(s0)
            # self.backend.add(s1)

        for readonly, args in self.readonly_backends:
            readonly.launch(**args)

            for robot in self.robots:
                readonly.add(robot, readonly=True)

        # Logging
        self.log_frequency = logging and 'frequency' in logging and logging['frequency']

    # def reset_backend(self):
    #     """
    #     Resets the backend correctly
    #     """
    #     # Check for error
    #     for robot in self.robots:
    #         self.backend.remove(robot)

    #     # for robot in self.robots:
    #     #     self.backend.add(robot)

    def close(self):
        """
        Close backend and stop action servers
        """
        self.backend.close()

        for robot in self.robots:
            robot.close()

    def publish_transforms(self) -> None:
        """[summary]
        """
        if not self.is_publishing_transforms:
            return

        transforms = []

        for robot in self.robots:
            joint_positions = getvector(robot.q, robot.n)

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
                    transform
                ))

            for gripper in robot.grippers:
                joint_positions = getvector(gripper.q, gripper.n)

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
                        transform
                    ))
        
        self.broadcaster.sendTransform(transforms)

    @staticmethod
    def load(path: str) -> Armer:
        """
        Generates an Armer Driver instance from the configuration file at path

        :param path: The path to the configuration file
        :type path: str
        :return: An Armer driver instance
        :rtype: Armer
        """
        with open(path, 'r') as handle:
            config = yaml.load(handle, Loader=yaml.SafeLoader)

        robots: List[rtb.robot.Robot] = []

        for spec in config['robots']:
            robot_cls = URDFRobot
            wrapper = ROSRobot

            model_spec = {}
            
            if 'model' in spec:
              model_type = spec['model'] if isinstance(spec['model'], str) else spec['model']['type'] if 'type' in spec['model'] else None
              model_spec = spec['model'] if isinstance(spec['model'], dict) else {}
              
              if model_type: 
                module_name, model_name = model_type.rsplit('.', maxsplit=1)            
                robot_cls = getattr(importlib.import_module(module_name), model_name)
                
              if 'type' in model_spec:
                del model_spec['type']     

              del spec['model']

            if 'type' in spec:
                module_name, model_name = spec['type'].rsplit('.', maxsplit=1)
                wrapper = getattr(importlib.import_module(module_name), model_name)
                del spec['type']

            robots.append(wrapper(robot_cls(**model_spec), **spec))

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
            robots=robots,
            backend=backend,
            backend_args=backend_args,
            readonly_backends=readonly_backends,
            publish_transforms=publish_transforms,
            logging=logging
        )

    def run(self) -> None:
        """
        Runs the driver. This is a blocking call.
        """
        self.last_tick = rospy.get_time()
        rospy.loginfo(f"ARMer Node Running...")
        
        while not rospy.is_shutdown():
            with Timer('ROS', self.log_frequency):
                current_time = rospy.get_time()
                dt = current_time - self.last_tick
                backend_reset = False

                # Step the robot(s)
                for robot in self.robots:
                    robot.step(dt=dt)

                    # Check if requested (resets overall for all robots in scene)
                    if robot.backend_reset: backend_reset = True

                # # Do a backend reset
                # if backend_reset:
                #     self.reset_backend()

                #     # Clear reset
                #     robot.backend_reset = False
                # else:
                # with Timer('step'):
                self.backend.step(dt=dt)

                for backend, args in self.readonly_backends:
                    backend.step(dt=dt)

                self.publish_transforms()

                self.rate.sleep()

                self.last_tick = current_time
