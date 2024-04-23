"""
ROSRobot module defines the ROSRobot type

.. codeauthor:: Gavin Suddreys
.. codeauthor:: Dasun Gunasinghe
"""
import copy
import os
import timeit
import json

from typing import List, Any
from threading import Lock, Event
from armer.timer import Timer
from armer.trajectory import TrajectoryExecutor
from armer.models import URDFRobot
import rospy
import actionlib
import tf
import roboticstoolbox as rtb
import spatialmath as sm
from spatialmath import SE3, SO3, UnitQuaternion
import pointcloud_utils as pclu
import numpy as np
import yaml
# Required for NEO
import qpsolvers as qp
import spatialgeometry as sg
from dataclasses import dataclass
from armer.utils import ikine, mjtg, trapezoidal

# Import Standard Messages
from std_msgs.msg import Header, Bool
from sensor_msgs.msg import JointState
from geometry_msgs.msg import Pose, PoseStamped, Point, Quaternion
from geometry_msgs.msg import TwistStamped, Twist, Transform
from std_srvs.srv import Empty, EmptyRequest, EmptyResponse, SetBool, SetBoolRequest, SetBoolResponse
from std_msgs.msg import Float64MultiArray

# Import ARMER Messages
from armer_msgs.msg import *
from armer_msgs.srv import *

# TESTING NEW IMPORTS FOR TRAJ DISPLAY 
from trajectory_msgs.msg import JointTrajectoryPoint, JointTrajectory

# TESTING NEW IMPORTS FOR DYNAMIC OBJECT MARKER DISPLAY (RVIZ)
from visualization_msgs.msg import Marker, MarkerArray, InteractiveMarker, InteractiveMarkerControl
from interactive_markers.interactive_marker_server import InteractiveMarkerServer, InteractiveMarkerFeedback

# TESTING CONTROLLER SWITCHING FOR NEW TRAJECTORY CONTROL
from controller_manager_msgs.srv import SwitchController
from control_msgs.msg import FollowJointTrajectoryAction, FollowJointTrajectoryGoal
# pylint: disable=too-many-instance-attributes

# TESTING NEW IMPORTS FOR KD TREE COLLISION SEARCH METHOD
from sklearn.neighbors import KDTree
import math

import tf2_ros

class ControlMode:
   ERROR=0
   JOINTS=1
   CARTESIAN=2

class ControllerType:
    JOINT_GROUP_VEL=0
    JOINT_TRAJECTORY=1

# Test class of dynamic objects
@dataclass
class DynamicCollisionObj:
    shape: sg.Shape
    key: str = ''
    id: int = 0
    pose: Pose = Pose()
    is_added: bool = False
    marker_created: bool = False

class ROSRobot(rtb.Robot):
    """The ROSRobot class wraps the rtb.Robot implementing basic ROS functionality
    """
    
    def __init__(self,
                 robot: rtb.robot.Robot,
                 name: str = None,
                 joint_state_topic: str = None,
                 joint_velocity_topic: str = None,
                 velocity_controller: str = None,
                 trajectory_controller: str = None,
                 origin=None,
                 config_path=None,
                 readonly=False,
                 frequency=None,
                 modified_qr=None,
                 singularity_thresh=0.02,
                 max_joint_velocity_gain=20.0,
                 max_cartesian_speed=2.0,
                 trajectory_end_cutoff=0.000001,
                 qlim_min=None,
                 qlim_max=None,
                 qdlim=None,
                 * args,
                 **kwargs):  # pylint: disable=unused-argument
        
        super().__init__(robot)
        self.__dict__.update(robot.__dict__)
        
        self.name = name if name else self.name
        self.readonly = readonly

        self.max_joint_velocity_gain = max_joint_velocity_gain
        self.max_cartesian_speed = max_cartesian_speed
        self.trajectory_end_cutoff = trajectory_end_cutoff

        self.tfBuffer = tf2_ros.Buffer()
        self.listener = tf2_ros.TransformListener(self.tfBuffer)
        self.backend_reset = False

        # Setup the names of each controller as configured by config file per robot
        # NOTE: this can be differently named based on the type of robot
        self.velocity_controller = velocity_controller
        self.trajectory_controller = trajectory_controller
        # DEBUGGING
        rospy.loginfo(f"[INITIALISATION] -> configured position controller name: {self.trajectory_controller}")
        rospy.loginfo(f"[INITIALISATION] -> configured joint vel controller name: {self.velocity_controller}")

        # Update with ustom qlim (joint limits) if specified in robot config
        if qlim_min and qlim_max:
            self.qlim = np.array([qlim_min, qlim_max])
            rospy.loginfo(f"[INITIALISATION] -> Updating Custom qlim: {self.qlim}")

        if qdlim:
            self.qdlim = np.array(qdlim)
            rospy.loginfo(f"[INITIALISATION] -> Updating Custom qdlim: {self.qdlim}")
        else:
            self.qdlim = None

        # Singularity index threshold (0 is a sigularity)
        # NOTE: this is a tested value and may require configuration (i.e., speed of robot)
        rospy.loginfo(f"[INITIALISATION] -> Singularity Scalar Threshold set to: {singularity_thresh}")
        self.singularity_thresh = singularity_thresh 
        self.manip_scalar = None
        self.singularity_approached = False
        
        if not hasattr(self, 'gripper'):
          self.gripper = self.grippers[0].name if len(self.grippers) > 0 else 'tool0'
          
        # Global list of links
        # NOTE: we currently only consider all links up to our configured self.gripper
        self.sorted_links=[]
        # Check if the existing gripper name exists (Error handling) otherwise default to top of dict stack
        if self.gripper not in self.link_dict.keys():
            default_top_link_name = sorted(self.link_dict.keys())[-1]
            rospy.logwarn(f"[INITIALISATION] -> Configured gripper name {self.gripper} not in link tree -> defaulting to top of stack: {default_top_link_name}")
            self.gripper = default_top_link_name

        # Sort links by parents starting from gripper
        link=self.link_dict[self.gripper]   
        
        # --- COLLISION HANDLING SETUP SECTION --- #
        # Loops through links (as read in through URDF parser)
        # Extracts each link's list of collision shapes (of type sg.Shape). TODO: add a validity check on type here
        # Updates a dictionary (key as link name) of collision shape lists per link
        # Create a new dictionary of link lookup to ignore overlapping joint/collision objects
        # NOTE: the theory here is to keep a record of all expected collisions (overlaps) per link so that
        #       the main self collision check can ignore these cases. 
        self.overlapped_link_dict = dict()
        self.collision_dict = dict()
        # This is a list of collision objects that is used by NEO
        # NOTE: this may not be explictly needed, as a list form (flattened) of the self.collision_dict.values() 
        #       contains the same information
        self.collision_obj_list = list()
        # Define a list of dynamic objects (to be added in at runtime)
        # NOTE: this is useful to include collision shapes (i.e., cylinders, spheres, cuboids) in your scene for collision checking
        self.dynamic_collision_dict = dict()
        self.dynamic_collision_removal_dict = dict()
        self.collision_approached = False
        # Flag toggled by service. Enables visualisation of target shapes in RVIZ 
        # NOTE: slower execution if enabled, so use only when necessary
        self.collision_debug_enabled = False
        # Iterate through robot links and sort - add to tracked collision list
        while link is not None:
            # Debugging
            # print(f"link name in sort: {link.name}")
            # Add current link to overall dictionary
            self.collision_dict[link.name] = link.collision.data if link.collision.data else []
            self.sorted_links.append(link)
            link=link.parent
        self.sorted_links.reverse()

        # Check for external links (from robot tree)
        # This is required to add any other links (not specifically part of the robot tree) 
        # to our collision dictionary for checking
        for link in self.links:
            # print(f"links: {link.name}")
            if link.name in self.collision_dict.keys():
                continue

            # print(f"adding link: {link.name} which is type: {type(link)} with collision data: {link.collision.data}")
            self.collision_dict[link.name] = link.collision.data if link.collision.data else []
            [self.collision_obj_list.append(data) for data in link.collision.data]
        
        # Handle collision link window slicing
        # Window slicing is a way to define which links need to be tracked for collisions (for efficiency)
        # Checks error of input from configuration file (note that default is current sorted links)
        self.collision_sliced_links = self.sorted_links
        self.update_link_collision_window()
        # Initialise a 'ghost' robot instance for trajectory collision checking prior to execution
        # NOTE: Used to visualise the trajectory as a simple marker list in RVIZ for debugging
        # NOTE: there was an issue identified around using the latest franka-emika panda description
        # NOTE: the urdf_rtb_path is set by URDFRobot on initialisation if a path to the toolbox descriptions is specified
        self.robot_ghost = URDFRobot(
            wait_for_description=False, 
            gripper=self.gripper,
            urdf_file=self.urdf_rtb_path,
            collision_check_start_link=self.collision_sliced_links[-1].name, 
            collision_check_stop_link=self.collision_sliced_links[0].name
        )
        # Debugging
        # print(f"All collision objects in main list: {self.collision_obj_list}")
        # print(f"Collision dict for links: {self.collision_dict}\n")
        # print(f"Dictionary of expected link collisions: {self.overlapped_link_dict}\n")    
        # print(f"Sliced Links: {[link.name for link in self.collision_sliced_links]}")

        # Collision Safe State Window
        # NOTE: the size of the window (default of 200) dictates how far back we move to get out
        # of a collision situation, based on previously executed joint states that were safe
        self.q_safe_window = np.zeros([200,len(self.q)])
        self.q_safe_window_p = 0

        # Setup interactive marker (for use with RVIZ)
        self.interactive_marker_server = InteractiveMarkerServer("armer_interactive_objects")
        # --- COLLISION HANDLING SETUP SECTION END--- #

        self.joint_indexes = []
        self.joint_names = list(map(lambda link: link._joint_name, filter(lambda link: link.isjoint, self.sorted_links)))
        
        if origin:
            self.base = SE3(origin[:3]) @ SE3.RPY(origin[3:])

        self.frequency = frequency if frequency else rospy.get_param((
          joint_state_topic if joint_state_topic else '/joint_states') + '/frequency', 500
        )
        
        self.q = self.qr if hasattr(self, 'qr') else self.q # pylint: disable=no-member
        if modified_qr:
            self.qr = modified_qr
            self.q = modified_qr

        # Joint state message
        self.joint_states = None

        # Guards used to prevent multiple motion requests conflicting
        self._controller_mode = ControlMode.JOINTS

        self.moving: bool = False
        self.last_moving: bool = False
        self.preempted: bool = False

        # Thread variables
        self.lock: Lock = Lock()
        self.event: Event = Event()

        # Arm state property
        self.state: ManipulatorState = ManipulatorState()

        # Expected cartesian velocity
        self.e_v_frame: str = None 

        # cartesian motion
        self.e_v: np.array = np.zeros(shape=(6,)) 
        # expected joint velocity
        self.j_v: np.array = np.zeros(
            shape=(len(self.q),)
        ) 

        self.e_p = self.fkine(self.q, start=self.base_link, end=self.gripper)

        self.last_update: float = 0
        self.last_tick: float = 0

        # Trajectory Generation (designed to expect a Trajectory class obj)
        self.executor = None
        self.traj_generator = trapezoidal #mjtg

        self.joint_subscriber = rospy.Subscriber(
            joint_state_topic if joint_state_topic else '/joint_states',
            JointState,
            self._state_cb
        )

        self.joint_velocity_topic = joint_velocity_topic \
                if joint_velocity_topic \
                else '/joint_group_velocity_controller/command'
        
        # Default to joint group velocity control
        self.controller_type = ControllerType.JOINT_GROUP_VEL
        self.controller_type_request = ControllerType.JOINT_GROUP_VEL
        self.jt_traj = JointTrajectory()
        self.hw_controlled = False

        if not self.readonly:
            # Create Transform Listener
            self.tf_listener = tf.TransformListener()

            # --- Setup Configuration for ARMer --- #
            # NOTE: this path (if not set in the cfg/<robot>_real.yaml) does not find the user when run
            #       with systemd. This param must be loaded in via the specific <robot>_real.yaml file.
            self.config_path = config_path if config_path else os.path.join(
                os.getenv('HOME', '/home'),
                '.ros/configs/system_named_poses.yaml'
            )
            self.custom_configs: List[str] = []
            self.__load_config()

            # Load in default path to collision scene
            self.collision_scene_default_path = os.path.join(
                os.getenv('HOME', '/home'),
                '.ros/configs/armer_collision_scene.yaml'
            )
            # Load the default path
            self.load_collision_scene_config()

            # --- Publishes trajectory to run as marker list --- #
            self.display_traj_publisher: rospy.Publisher = rospy.Publisher(
                '{}/trajectory_display'.format(self.name.lower()),
                Marker,
                queue_size=1
            )

            # --- Publishes marker shapes as current target for checking collisions --- #
            self.collision_debug_publisher: rospy.Publisher = rospy.Publisher(
                '{}/collision_debug_display'.format(self.name.lower()),
                MarkerArray,
                queue_size=1
            )

            # --- ROS Publisher Setup --- #
            self.joint_publisher: rospy.Publisher = rospy.Publisher(
                self.joint_velocity_topic,
                Float64MultiArray,
                queue_size=1
            )
            self.state_publisher: rospy.Publisher = rospy.Publisher(
                '{}/state'.format(self.name.lower()), 
                ManipulatorState, 
                queue_size=1
            )
            self.cartesian_servo_publisher: rospy.Publisher = rospy.Publisher(
                '{}/cartesian/servo/arrived'.format(self.name.lower()), 
                Bool, 
                queue_size=1
            )

            # --- ROS Subscriber Setup --- #
            self.cartesian_velocity_subscriber: rospy.Subscriber = rospy.Subscriber(
                '{}/cartesian/velocity'.format(self.name.lower()), 
                TwistStamped, 
                self.velocity_cb
            )
            self.joint_velocity_subscriber: rospy.Subscriber = rospy.Subscriber(
                '{}/joint/velocity'.format(self.name.lower()), 
                JointVelocity, 
                self.joint_velocity_cb
            )
            self.cartesian_servo_subscriber: rospy.Subscriber = rospy.Subscriber(
                '{}/cartesian/servo'.format(self.name.lower()), 
                ServoStamped, 
                self.servo_cb
            )
            self.set_pid_subscriber: rospy.Subscriber = rospy.Subscriber(
                '{}/set_pid'.format(self.name.lower()),
                Float64MultiArray,
                self.set_pid
            )

            # --- ROS Action Server Setup --- #
            self.velocity_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/cartesian/guarded_velocity'.format(self.name.lower()),
                GuardedVelocityAction,
                execute_cb=self.guarded_velocity_cb,
                auto_start=False
            )
            self.velocity_server.register_preempt_callback(self.preempt)
            self.velocity_server.start()

            self.pose_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/cartesian/pose'.format(self.name.lower()),
                MoveToPoseAction,
                execute_cb=self.pose_cb,
                auto_start=False
            )
            self.pose_server.register_preempt_callback(self.preempt)
            self.pose_server.start()

            self.step_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/cartesian/step'.format(self.name.lower()),
                MoveToPoseAction,
                execute_cb=self.step_cb,
                auto_start=False
            )
            self.step_server.register_preempt_callback(self.preempt)
            self.step_server.start()

            self.pose_tracking_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/cartesian/track_pose'.format(self.name.lower()),
                TrackPoseAction,
                execute_cb=self.pose_tracker_cb,
                auto_start=False,
            )
            self.pose_tracking_server.register_preempt_callback(self.preempt_tracking)
            self.pose_tracking_server.start()

            # TODO: Remove this action server - just for debugging
            self.tf_to_pose_transporter_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/pose_from_tf'.format(self.name.lower()),
                TfToPoseAction,
                execute_cb=self.tf_to_pose_transporter_cb,
                auto_start=False,
            )
            self.tf_to_pose_transporter_server.register_preempt_callback(self.preempt_other)
            self.tf_to_pose_transporter_server.start()

            self.joint_pose_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/joint/pose'.format(self.name.lower()),
                MoveToJointPoseAction,
                execute_cb=self.joint_pose_cb,
                auto_start=False
            )
            self.joint_pose_server.register_preempt_callback(self.preempt)
            self.joint_pose_server.start()

            self.named_pose_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/joint/named'.format(self.name.lower()),
                MoveToNamedPoseAction,
                execute_cb=self.named_pose_cb,
                auto_start=False
            )
            self.named_pose_server.register_preempt_callback(self.preempt)
            self.named_pose_server.start()

            self.named_pose_in_frame_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/joint/named_in_frame'.format(self.name.lower()),
                MoveToNamedPoseAction,
                execute_cb=self.named_pose_in_frame_cb,
                auto_start=False
            )
            self.named_pose_in_frame_server.register_preempt_callback(self.preempt)
            self.named_pose_in_frame_server.start()

            self.named_pose_distance_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/measurement/named_to_gripper'.format(self.name.lower()),
                MoveToNamedPoseAction,
                execute_cb=self.named_pose_distance_cb,
                auto_start=False
            )
            self.named_pose_distance_server.register_preempt_callback(self.preempt)
            self.named_pose_distance_server.start()

            self.home_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/home'.format(self.name.lower()),
                HomeAction,
                execute_cb=self.home_cb,
                auto_start=False
            )
            self.home_server.register_preempt_callback(self.preempt)
            self.home_server.start()

            # --- ROS Services Setup --- #            
            rospy.Service(
                '{}/recover'.format(self.name.lower()),
                Empty, 
                self.recover_cb
            )

            rospy.Service(
                '{}/recover_move'.format(self.name.lower()),
                Empty,
                self.recover_move_cb
            )

            rospy.Service(
                '{}/stop'.format(self.name.lower()),
                Empty, 
                self.preempt
            )
            
            rospy.Service(
                '{}/set_cartesian_impedance'.format(self.name.lower()),
                SetCartesianImpedance,
                self.set_cartesian_impedance_cb
            )

            rospy.Service(
                '{}/get_ee_link_name'.format(self.name.lower()),
                GetLinkName,
                lambda req: GetLinkNameResponse(name=self.gripper)
            )

            rospy.Service(
                '{}/get_base_link_name'.format(self.name.lower()),
                GetLinkName,
                lambda req: GetLinkNameResponse(name=self.base_link.name)
            )

            rospy.Service(
                '{}/update_description'.format(self.name.lower()),
                UpdateDescription,
                self.update_description_cb
            )

            rospy.Service(
                '{}/calibrate_transform'.format(self.name.lower()),
                CalibrateTransform,
                self.calibrate_transform_cb
            )

            rospy.Service(
                '{}/get_named_poses'.format(self.name.lower()), 
                GetNamedPoses,
                self.get_named_poses_cb
            )
            
            rospy.Service(
                '{}/set_named_pose'.format(self.name.lower()), 
                AddNamedPose,
                self.add_named_pose_cb
            )

            rospy.Service(
                '{}/set_named_pose_in_frame'.format(self.name.lower()), 
                AddNamedPoseInFrame,
                self.add_named_pose_in_frame_cb
            )
            
            rospy.Service(
                '{}/remove_named_pose'.format(self.name.lower()), 
                RemoveNamedPose,
                self.remove_named_pose_cb
            )
            
            rospy.Service(
                '{}/export_named_pose_config'.format(self.name.lower()),
                NamedPoseConfig,
                self.export_named_pose_config_cb
            )

            rospy.Service(
                '{}/add_named_pose_config'.format(self.name.lower()),
                NamedPoseConfig,
                self.add_named_pose_config_cb
            )

            rospy.Service(
                '{}/remove_named_pose_config'.format(self.name.lower()),
                NamedPoseConfig,
                self.remove_named_pose_config_cb
            )
            
            rospy.Service(
                '{}/get_named_pose_configs'.format(self.name.lower()),
                GetNamedPoseConfigs,
                self.get_named_pose_configs_cb
            )

            # ------ Collision Checking Services ----- #
            rospy.Service(
                '{}/add_collision_object'.format(self.name.lower()),
                AddCollisionObject,
                self.add_collision_obj_cb
            )

            rospy.Service(
                '{}/remove_collision_object'.format(self.name.lower()),
                RemoveCollisionObject,
                self.remove_collision_obj_cb
            )

            rospy.Service(
                '{}/get_collision_objects'.format(self.name.lower()),
                GetCollisionObjects,
                self.get_collision_obj_cb
            )
            
            rospy.Service(
                '{}/enable_collision_debug'.format(self.name.lower()),
                SetBool,
                self.enable_collision_debug_cb
            )

            rospy.Service(
                '{}/save_collision_objects'.format(self.name.lower()),
                CollisionSceneConfig,
                self.save_collision_config_cb
            )

            rospy.Service(
                '{}/load_collision_config_path'.format(self.name.lower()),
                CollisionSceneConfig,
                self.load_collision_config_path_cb
            )

    # --------------------------------------------------------------------- #
    # --------- ROS Topic Callback Methods -------------------------------- #
    # --------------------------------------------------------------------- #
    def _state_cb(self, msg):
        """Updates the current joint state of the robot (from hardware)

        :param msg: Message containing joint data
        :type msg: JointState
        """
        if not self.joint_indexes:
            for joint_name in self.joint_names:
                self.joint_indexes.append(msg.name.index(joint_name))
        
        self.q = np.array(msg.position)[self.joint_indexes] if len(msg.position) == self.n else np.zeros(self.n)
        self.joint_states = msg
        
    def velocity_cb(self, msg: TwistStamped) -> None:
        """ROS velocity callback:
        Moves the arm at the specified cartesian velocity
        w.r.t. a target frame

        :param msg: [description]
        :type msg: TwistStamped
        """
        if self._controller_mode == ControlMode.ERROR:
            rospy.logerr_throttle(msg=f"[CART VELOCITY CB] -> [{self.name}] in Error Control Mode...", period=1)
            return None

        if self.moving:
            self.preempt()

        with self.lock:
            self.preempted = False
            self.__vel_move(msg)

    def joint_velocity_cb(self, msg: JointVelocity) -> None:
        """ROS joint velocity callback:
        Moves the joints of the arm at the specified velocities

        :param msg: [description]
        :type msg: JointVelocity
        """
        if self._controller_mode == ControlMode.ERROR:
            rospy.logerr_throttle(msg=f"[JOINT VELOCITY CB] -> [{self.name}] in Error Control Mode...", period=1)
            return None

        # Check for vector length and terminate if invalid
        if len(msg.joints) != len(self.j_v):
            rospy.logerr_throttle(msg=f"[JOINT VELOCITY CB] -> [{self.name}] provided input vector length invalid [{len(msg.joints)}]. Expecting len [{len(self.j_v)}]", period=1)
            return None
         
        if self.moving:            
            self.preempt()

        with self.lock:
            self.j_v = np.array(msg.joints)
            self.last_update = rospy.get_time()

    # --------------------------------------------------------------------- #
    # --------- Traditional ROS Action Callback Methods ------------------- #
    # --------------------------------------------------------------------- #
    def guarded_velocity_cb(self, msg: GuardedVelocityGoal) -> None:
        """ROS Guarded velocity callback
        Moves the end-effector in cartesian space with respect to guards (time or force)
        
        :param msg: [description]
        :type msg: GuardedVelocityGoal
        """
        if self.moving:
            self.preempt()
        
        with self.lock:
            self.preempted = False
            
            start_time = rospy.get_time()
            triggered = 0
            
            while not self.preempted:
                triggered = self.test_guards(msg.guards, start_time=start_time)

                if triggered != 0:
                    break

                self.__vel_move(msg.twist_stamped)
                rospy.sleep(0.01)

            if not self.preempted:
                self.velocity_server.set_succeeded(GuardedVelocityResult(triggered=triggered))
            else:
                self.velocity_server.set_aborted(GuardedVelocityResult())

    def servo_cb(self, msg) -> None:
        """ROS Servoing Action Callback:
        Servos the end-effector to the cartesian pose given by msg
        
        :param msg: [description]
        :type msg: ServoStamped

        This callback makes use of the roboticstoolbox p_servo function
        to generate velocities at each timestep.
        """
        if self._controller_mode == ControlMode.ERROR:
            rospy.logerr_throttle(msg=f"SERVO CB: [{self.name}] in Error Control Mode...", period=1)
            return None
        
        # Safely stop any current motion of the arm
        if self.moving:
            self.preempt()
        
        with self.lock:
            # Handle variables for servo
            goal_pose = msg.pose
            goal_gain = msg.gain if msg.gain else 0.2
            goal_thresh = msg.threshold if msg.threshold else 0.005
            arrived = False
            # self.moving = True
            # self.preempted = False

            # Current end-effector pose
            Te = self.ets(start=self.base_link, end=self.gripper).eval(self.q)

            # Handle frame id of servo request
            if msg.header.frame_id == '':
                msg.header.frame_id = self.base_link.name
            
            goal_pose_stamped = self.tf_listener.transformPose(
                self.base_link.name,
                PoseStamped(header=msg.header, pose=goal_pose)
            )
            pose = goal_pose_stamped.pose

            # Convert target to SE3 (from pose)
            target = SE3(pose.position.x, pose.position.y, pose.position.z) * UnitQuaternion([
                pose.orientation.w,
                pose.orientation.x,
                pose.orientation.y,
                pose.orientation.z
            ]).SE3()

            # Calculate the required end-effector spatial velocity for the robot
            # to approach the goal.
            velocities, arrived = rtb.p_servo(
                Te,
                target,
                min(20, goal_gain),
                threshold=goal_thresh
            )

            ##### TESTING NEO IMPLEMENTATION #####
            # neo_jv = self.neo(Tep=target, velocities=velocities)
            neo_jv = None

            if np.any(neo_jv):
                self.j_v = neo_jv[:len(self.q)]
            else:
                self.j_v = np.linalg.pinv(self.jacobe(self.q)) @ velocities

            # print(f"current jv: {self.j_v} | updated neo jv: {neo_jv}")
            self.last_update = rospy.get_time()

        self.cartesian_servo_publisher.publish(arrived)

    def pose_cb(self, goal: MoveToPoseGoal) -> None:
        """ROS Action Server callback:
        Moves the end-effector to the
        cartesian pose indicated by goal

        :param goal: [description]
        :type goal: MoveToPoseGoal
        """
        if self.moving:
            self.preempt()

        with self.lock:
            goal_pose = goal.pose_stamped

            if goal_pose.header.frame_id == '':
                goal_pose.header.frame_id = self.base_link.name

            goal_pose = self.tf_listener.transformPose(
                self.base_link.name,
                goal_pose,
            )
            pose = goal_pose.pose

            # Handle zero time input (or negative)
            move_time = goal.time 
            if goal.time <= 0.0:
                # Default to 5 seconds
                move_time = 5.0
            
            # Attempt to get valid solution
            # NOTE: on failure, returns existing state as solution for 0 movement
            solution = ikine(self, pose, q0=self.q, end=self.gripper)
            ik_invalid = all(x == y for x,y in zip(self.q, solution.q))
            if ik_invalid:
                rospy.logwarn(f"[CARTESIAN POSE MOVE CB] -> Could not find valid IK solution, refusing to move...")
                self.pose_server.set_succeeded(MoveToPoseResult(success=False), 'IK Solution Invalid')
            else:
                # NOTE: checks and preempts if collision or workspace violation
                # NOTE: can proceed if workspace not defined
                if self.general_executor(q=solution.q, pose=pose, collision_ignore=False, workspace_ignore=False, move_time_sec=move_time):
                    self.pose_server.set_succeeded(MoveToPoseResult(success=True))
                else:
                    self.pose_server.set_aborted(MoveToPoseResult(success=False), 'Executor Failed in Action')
                
            # Reset Flags at End
            self.executor = None
            self.moving = False
    
    def joint_pose_cb(self, goal: MoveToJointPoseGoal) -> None:
        """ROS Action Server callback:
        Moves the arm the named pose indicated by goal

        :param goal: Goal message containing the name of the joint configuration to which the arm should move
        :type goal: MoveToNamedPoseGoal
        """
        if self.moving:
            self.preempt()

        with self.lock:     
            # Handle zero time input (or negative)
            move_time = goal.time 
            if goal.time <= 0.0:
                # Default to 5 seconds
                move_time = 5.0

            # NOTE: checks for collisions and workspace violations
            # NOTE: checks for singularity violations
            # NOTE: can continue if workspace is not defined (ignored as no pose is defined)
            if self.general_executor(q=goal.joints, workspace_ignore=True, move_time_sec=move_time):
                self.joint_pose_server.set_succeeded(MoveToJointPoseResult(success=True))
            else:
                self.joint_pose_server.set_aborted(MoveToJointPoseResult(success=False))

            self.executor = None
            self.moving = False

    def named_pose_cb(self, goal: MoveToNamedPoseGoal) -> None:
        """ROS Action Server callback:
        Moves the arm the named pose indicated by goal

        :param goal: Goal message containing the name of the joint configuration to which the arm should move
        :type goal: MoveToNamedPoseGoal
        """
        if self.moving:
            self.preempt()

        with self.lock:
            if not goal.pose_name in self.named_poses:
                self.named_pose_server.set_aborted(
                    MoveToNamedPoseResult(success=False),
                    'Unknown named pose'
                )
                rospy.logwarn(f"-- Named pose goal ({goal.pose_name}) is unknown; refusing to move...")
                return

            # Extract end state (q) of joints
            q = np.array(self.named_poses[goal.pose_name])

            # Calculate end pose for checking boundary (if defined)
            goal_pose_se3 = SE3(self.ets(start=self.base_link, end=self.gripper).eval(q))
            goal_pose = Pose()
            goal_pose.position.x = goal_pose_se3.t[0]
            goal_pose.position.y = goal_pose_se3.t[1]
            goal_pose.position.z = goal_pose_se3.t[2]

            # Handle zero time input (or negative)
            move_time = goal.time 
            if goal.time <= 0.0:
                # Default to 5 seconds
                move_time = 5.0

            # NOTE: Checks collisions prior to executing
            # NOTE: Checks workspace (if defined), continues if not
            if self.general_executor(q=q, pose=goal_pose, move_time_sec=move_time):
                self.named_pose_server.set_succeeded(MoveToNamedPoseResult(success=True))
            else:
                self.named_pose_server.set_aborted(MoveToNamedPoseResult(success=False))

            # Reset of flags at end
            self.executor = None
            self.moving = False

    def home_cb(self, goal: HomeGoal) -> HomeResult:
        """[summary]

        :param req: Empty request
        :type req: EmptyRequest
        :return: Empty response
        :rtype: EmptyResponse
        """
        if self.moving:
            self.preempt()
            
        with self.lock:
            # Prep end goal state (q) for home joint positions
            q = np.array(self.qr) if hasattr(self, 'qr') else self.q
            
            # Handle zero time input (or negative)
            move_time = goal.time 
            if goal.time <= 0.0:
                # Default to 5 seconds
                move_time = 5.0

            # Run the general executor (checks for collisions)
            # NOTE: ignores workspace on homing
            if self.general_executor(q=q, workspace_ignore=True, move_time_sec=move_time):
                self.home_server.set_succeeded(HomeResult(success=True))
            else:
                self.home_server.set_aborted(HomeResult(success=False))

            # Recover from Error if set
            if self._controller_mode == ControlMode.ERROR:
                rospy.loginfo(f"Resetting from ERROR state to JOINTS [Default]")
                self._controller_mode = ControlMode.JOINTS
                self.preempted = False

            self.executor = None
            self.moving = False

    # --------------------------------------------------------------------- #
    # --------- Added Custom ROS Action Callback Methods ------------------ #
    # --------------------------------------------------------------------- #
    def tf_to_pose_transporter_cb(self, goal: TfToPoseGoal) -> None:
        pub_rate = rospy.Rate(goal.rate)
        pub = rospy.Publisher(goal.pose_topic, PoseStamped, queue_size=1)
        pub_target = rospy.Publisher("/target_target", Pose, queue_size=1)
        pub_ee = rospy.Publisher("/target_ee", Pose, queue_size=1)

        rospy.logerr("Updating TF to Pose publisher...")

        previous_pose = Pose()

        while not self.tf_to_pose_transporter_server.is_preempt_requested():

            if goal.target_tf == None or goal.target_tf == "":
                rospy.logerr(f"Provided target tf is None")
                pub_rate.sleep()
                continue

            ee_pose = Pose()
            target_pose_offset = Pose()
            try:
                self.tf_listener.waitForTransform(self.base_link.name, "ee_control_link", rospy.Time.now(), rospy.Duration(1.0))
                ee_pose = pclu.ROSHelper().tf_to_pose(self.base_link.name, "ee_control_link", self.tfBuffer)
                self.tf_listener.waitForTransform(goal.ee_frame, goal.target_tf, rospy.Time.now(), rospy.Duration(1.0))
                target_pose_offset = pclu.ROSHelper().tf_to_pose(goal.ee_frame, goal.target_tf, self.tfBuffer)
            except:
                rospy.logerr(f"failed to get transforms...")
                if previous_pose != Pose():
                    pub.publish(previous_pose)
                    rospy.logerr(f"...previous_pose available")
                elif ee_pose != Pose():
                    previous_pose = PoseStamped()
                    previous_pose.header.stamp = rospy.Time.now()
                    previous_pose.header.frame_id = self.base_link.name
                    previous_pose.pose = ee_pose
                    pub.publish(previous_pose)
                    rospy.logerr(f"...ee_pose available")
                pub_rate.sleep()
                continue
                    
            if target_pose_offset == Pose():
                rospy.logerr(f"target vector is empty, cannot be calculated. exiting...")
                if previous_pose != Pose():
                    pub.publish(previous_pose)
                elif ee_pose != Pose():
                    previous_pose = PoseStamped()
                    previous_pose.header.stamp = rospy.Time.now()
                    previous_pose.header.frame_id = self.base_link.name
                    previous_pose.pose = ee_pose
                    pub.publish(previous_pose)
                pub_rate.sleep()
                continue

            target_rotation = sm.UnitQuaternion(
                target_pose_offset.orientation.w, [
                target_pose_offset.orientation.x,
                target_pose_offset.orientation.y,
                target_pose_offset.orientation.z
            ])

            ee_rotation = sm.UnitQuaternion(
                ee_pose.orientation.w, [
                ee_pose.orientation.x,
                ee_pose.orientation.y,
                ee_pose.orientation.z
            ])

            # SO3 representations
            target_rot_so = sm.SO3.RPY(target_rotation.rpy(order='xyz')) # Working...
            ee_rot_so = sm.SO3.RPY(ee_rotation.rpy(order='zyx'))

            goal_rotation = sm.UnitQuaternion(ee_rot_so * target_rot_so.inv()) # works perfect with neg R & P

            # Hack test to negate R & P should apply rotation
            goal_rpy = goal_rotation.rpy()
            goal_rotation = sm.UnitQuaternion(sm.SO3.RPY(-goal_rpy[0], -goal_rpy[1], goal_rpy[2]))

            goal_pose = copy.deepcopy(ee_pose)
            goal_pose.orientation.w = goal_rotation.s
            goal_pose.orientation.x = goal_rotation.v[0]
            goal_pose.orientation.y = goal_rotation.v[1]
            goal_pose.orientation.z = goal_rotation.v[2]

            # This is a hack...fix it...
            # NOTE: this assumes target is in the camera frame...
            # Use SE3 and apply the above rotations
            goal_pose.position.x += target_pose_offset.position.y
            goal_pose.position.y += target_pose_offset.position.z
            goal_pose.position.z += target_pose_offset.position.x

            rospy.logwarn(f"TF to Pose")
            rospy.logwarn(f"-- Current EE Pose AS RPY {ee_rot_so.rpy()}")
            rospy.logwarn(f"-- Target AS RPY {target_rot_so.rpy()}")
            rospy.logwarn(f"-- RESULT AS RPY {goal_rotation.rpy()}")
            rospy.logwarn(f"-- Goal EE Pose {goal_pose}")

            pub_target.publish(target_pose_offset)
            pub_ee.publish(ee_pose)

            goal_pose_msg = PoseStamped()
            goal_pose_msg.header.stamp = rospy.Time.now()
            goal_pose_msg.header.frame_id = self.base_link.name
            goal_pose_msg.pose = goal_pose
            pub.publish(goal_pose_msg)

            previous_pose = copy.deepcopy(goal_pose_msg)

            # break
            pub_rate.sleep()

        self.tf_to_pose_transporter_server.set_succeeded(
            TfToPoseResult(success=True))

    def pose_tracker_cb(self, goal: TrackPoseGoal) -> None:
        """
        ROS Action Server callback:
        Moves the end-effector to the
        cartesian pose defined by the supplied PoseStamped topic
        """
        msg_pose_publish_rate = 10
        pub_rate = rospy.Rate(msg_pose_publish_rate)

        # TODO: Refactor - this should be used both for distance to goal and for new pose validation
        msg_pose_threshold = 0.005

        msg_pose_topic = goal.tracked_pose_topic

        # TODO: Remove debugging
        feedback = TrackPoseFeedback()
        feedback.status = 0
        self.pose_tracking_server.publish_feedback(feedback)

        if self.moving:
            self.preempt()
            self.moving = False

        with self.lock:
            self.preempted = False

            tracked_pose = None

            # while not self.preempted: 
            while not self.pose_tracking_server.is_preempt_requested():

                if self.preempted:
                    # Reset the board
                    self.preempted = False
                    # - do some other things...like control_type

                pose_msg = None
                try:
                    pose_msg = rospy.wait_for_message(topic=msg_pose_topic, topic_type=PoseStamped, timeout=0.1)
                    # Apply offset provided to pose_msg
                    pose_msg.pose.position.x += goal.pose_offset_x
                    pose_msg.pose.position.y += goal.pose_offset_y
                    pose_msg.pose.position.z += goal.pose_offset_z

                    tracked_pose = copy.deepcopy(pose_msg)
                except:
                    pose_msg = tracked_pose

                if pose_msg:
                    goal_pose = pose_msg
                elif tracked_pose is None:
                    # Not currently tracking
                    pub_rate.sleep()
                    continue

                # TODO: Remove debugging
                feedback = TrackPoseFeedback()
                feedback.status = 2
                self.pose_tracking_server.publish_feedback(feedback)

                # TODO: Check if we bother processing the goal_pose given the tracked_pose distance
                tracked_pose = goal_pose

                # IS THE GOAL POSE WITHIN THE BOUNDRY?...
                if self.pose_within_workspace(goal_pose.pose) == False:
                    pub_rate.sleep()
                    continue

                if goal.linear_motion:
                    # Method 2: Use Servo to Pose
                    # Handle variables for servo
                    goal_gain = goal.vel_scale * self.max_joint_velocity_gain if goal.vel_scale else 0.2
                    goal_thresh = msg_pose_threshold if msg_pose_threshold else 0.005
                    arrived = False
                    self.moving = True

                    # Current end-effector pose
                    Te = self.ets(start=self.base_link, end=self.gripper).eval(self.q)

                    pose = goal_pose.pose

                    # Overwrite provided pose orientation if required
                    if goal.gripper_orientation_lock:
                        pose.orientation = self.state.ee_pose.pose.orientation

                    # Convert target to SE3 (from pose)
                    target = SE3(pose.position.x, pose.position.y, pose.position.z) * UnitQuaternion(
                        pose.orientation.w,
                        [pose.orientation.x,
                        pose.orientation.y,
                        pose.orientation.z
                    ]).SE3()

                    # Calculate the required end-effector spatial velocity for the robot
                    # to approach the goal.
                    velocities, arrived = rtb.p_servo(
                        Te,
                        target,
                        min(20, goal_gain),
                        threshold=goal_thresh
                    )

                    ## TODO: Remove this or NEO Testing...
                    self.j_v = np.linalg.pinv(self.jacobe(self.q)) @ velocities
                    self.last_update = rospy.get_time()

                    # ##### TESTING NEO IMPLEMENTATION #####
                    # # neo_jv = self.neo(Tep=target, velocities=velocities)
                    # neo_jv = None

                    # if np.any(neo_jv):
                    #     self.j_v = neo_jv[:len(self.q)]
                    # else:
                    #     self.j_v = np.linalg.pinv(self.jacobe(self.q)) @ velocities

                    # # print(f"current jv: {self.j_v} | updated neo jv: {neo_jv}")
                    # self.last_update = rospy.get_time()

                    # # if arrived == True:
                    # #     break

                    pub_rate.sleep()
                    # # ---- END Method 2 ---------------------------------
                else:
                    # Method 1: Use TrajectoryExecutor
                    goal_speed = goal.vel_scale * self.max_cartesian_speed if goal.vel_scale else 0.2
                    if goal_pose.header.frame_id == '':
                        goal_pose.header.frame_id = self.base_link.name

                    goal_pose = self.tf_listener.transformPose(
                        self.base_link.name,
                        goal_pose,
                    )

                    pose = goal_pose.pose

                    # Test Distance to goal and for active motion
                    # TODO: Add pose comparison
                    if self.executor is not None:
                        if not self.executor.is_finished(cutoff=msg_pose_threshold):
                            pub_rate.sleep()
                            continue
                        # if not self.executor.is_succeeded():
                        #     self.executor = None
                        #     pub_rate.sleep()
                        #     continue

                    solution = None
                    try:
                        solution = ikine(self, pose, q0=self.q, end=self.gripper)
                    except:
                        rospy.logwarn("Failed to get IK Solution...")

                    if solution is None:
                        pub_rate.sleep()
                        continue

                #     # Check for singularity on end solution:
                #     # TODO: prevent motion on this bool? Needs to be thought about
                #     if self.check_singularity(solution.q):
                #         rospy.logwarn(f"IK solution within singularity threshold [{self.singularity_thresh}] -> ill-advised motion")

                    try:
                        self.executor = TrajectoryExecutor(
                        self,
                        self.traj_generator(self, solution.q, goal_speed),
                        cutoff=self.trajectory_end_cutoff
                        )
                    except:
                        rospy.logwarn("TrackPose - Unable to construct TrajectoryExecutor")
                        pub_rate.sleep()
                        continue

                    pub_rate.sleep()
                    # ---- END Method 1 ---------------------------------

        # TODO: Remove debugging
        feedback = TrackPoseFeedback()
        feedback.status = 3
        self.pose_tracking_server.publish_feedback(feedback)

        if not self.preempted:
            # TODO: Remove debugging
            feedback = TrackPoseFeedback()
            feedback.status = 33
            self.pose_tracking_server.publish_feedback(feedback)

            self.pose_tracking_server.set_succeeded(TrackPoseResult(success=True))
        else:
            # TODO: Remove debugging
            feedback = TrackPoseFeedback()
            feedback.status = 34
            self.pose_tracking_server.publish_feedback(feedback)

            self.pose_tracking_server.set_aborted(TrackPoseResult(success=False))

        self.executor = None
        self.moving = False

    def step_cb(self, goal: MoveToPoseGoal) -> None:
        """
        """
        if self.moving:
            self.preempt()
            self.moving = False

        with self.lock:
            arrived = False
            self.moving = True
            
            goal_pose = goal.pose_stamped

            if goal_pose.header.frame_id == '':
                goal_pose.header.frame_id = self.base_link.name

            # Get the EE pose in the armer defined base_link
            ee_pose = self.ets(start=self.base_link, end=self.gripper).eval(self.q.tolist())
            ee_pose_stamped = PoseStamped()
            ee_pose_stamped.header.frame_id = self.base_link.name

            translation = ee_pose[:3, 3]
            ee_pose_stamped.pose.position.x = translation[0]
            ee_pose_stamped.pose.position.y = translation[1]
            ee_pose_stamped.pose.position.z = translation[2]

            rotation = ee_pose[:3, :3]
            ee_rot = sm.UnitQuaternion(rotation)
            ee_pose_stamped.pose.orientation.w = ee_rot.A[0]
            ee_pose_stamped.pose.orientation.x = ee_rot.A[1]
            ee_pose_stamped.pose.orientation.y = ee_rot.A[2]
            ee_pose_stamped.pose.orientation.z = ee_rot.A[3]

            # Transform EE to goal frame
            ee_in_goal = self.tf_listener.transformPose(
                goal.pose_stamped.header.frame_id,
                ee_pose_stamped,
            )

            # Apply goal step
            step_pose_stamped = copy.deepcopy(goal_pose)
            step_pose_stamped.pose.position.x += ee_in_goal.pose.position.x
            step_pose_stamped.pose.position.y += ee_in_goal.pose.position.y
            step_pose_stamped.pose.position.z += ee_in_goal.pose.position.z

            # NOTE: Ignore orientation until an action message is made with degrees for user convenience
            step_pose_stamped.pose.orientation.w = ee_in_goal.pose.orientation.w
            step_pose_stamped.pose.orientation.x = ee_in_goal.pose.orientation.x
            step_pose_stamped.pose.orientation.y = ee_in_goal.pose.orientation.y
            step_pose_stamped.pose.orientation.z = ee_in_goal.pose.orientation.z

            # Transform step_pose to armer base_link
            step_pose_stamped = self.tf_listener.transformPose(
                self.base_link.name,
                step_pose_stamped,
            )
            step_pose = step_pose_stamped.pose

            # IS THE GOAL POSE WITHIN THE BOUNDRY?...
            if self.pose_within_workspace(step_pose) == False:
                rospy.logwarn("-- Pose goal outside defined workspace; refusing to move...")
                self.step_server.set_succeeded(
                  MoveToPoseResult(success=False), 'Named pose outside defined workspace'
                )
                self.executor = None
                self.moving = False
                return
            
            # TODO: Refactor - this provided as a parameter
            msg_pose_threshold = 0.005
            
            if goal.linear_motion:
                rospy.logwarn('Moving in linear motion mode...')
                # Method 2: Use Servo to Pose
                # Handle variables for servo
                goal_gain = goal.speed * self.max_joint_velocity_gain if goal.speed else 0.02 * self.max_joint_velocity_gain
                goal_thresh = msg_pose_threshold if msg_pose_threshold else 0.005

                # Target is just the pose delta provided (orientation currently ignored - remains at current orientation)
                target = SE3(step_pose.position.x, step_pose.position.y, step_pose.position.z) * UnitQuaternion(
                    ee_rot.A[0],
                    [ee_rot.A[1],
                    ee_rot.A[2],
                    ee_rot.A[3]
                ]).SE3()

                rospy.logdebug(f'Goal Pose: {goal_pose.pose}')
                rospy.logdebug(f'Step Pose: {step_pose}')
                rospy.logdebug(f'Target Pose: {target}')

                # Block while move is completed
                while arrived == False and not self.step_server.is_preempt_requested():
                    # Current end-effector pose
                    Te = self.ets(start=self.base_link, end=self.gripper).eval(self.q)

                    # Calculate the required end-effector spatial velocity for the robot
                    # to approach the goal.
                    velocities, arrived = rtb.p_servo(
                        Te,
                        target,
                        min(20, goal_gain),
                        threshold=goal_thresh
                    )

                    # TODO: Investigate / Validate returned arrived boolean from RTB
                    # - default currently is RPY method
                    # - arrived is not arrived (sum of errors < threshold)
                    # - may also mix spatial and angle errors

                    ## TODO: Remove this or NEO Testing...
                    self.j_v = np.linalg.pinv(self.jacobe(self.q)) @ velocities
                    self.last_update = rospy.get_time()
                    rospy.sleep(0.1)

            else:
                solution = ikine(self, step_pose, q0=self.q, end=self.gripper)

                # Check for singularity on end solution:
                # TODO: prevent motion on this bool? Needs to be thought about
                if self.check_singularity(solution.q):
                    rospy.logwarn(f"IK solution within singularity threshold [{self.singularity_thresh}] -> ill-advised motion")
                
                self.executor = TrajectoryExecutor(
                    self,
                    self.traj_generator(self, solution.q, goal.speed if goal.speed else 0.2),
                    cutoff=self.trajectory_end_cutoff
                    )

                # Block while move is completed
                while not self.executor.is_finished() and not self.step_server.is_preempt_requested():
                    rospy.sleep(0.01)

            if (self.executor is not None and self.executor.is_succeeded()) or arrived == True:
                self.step_server.set_succeeded(MoveToPoseResult(success=True))
                rospy.logwarn('...Motion Complete!')
            else:
                self.step_server.set_aborted(MoveToPoseResult(success=False))
                rospy.logwarn('...Failed Motion!')

            # Clean up
            self.executor = None
            self.moving = False

    def named_pose_in_frame_cb(self, goal: MoveToNamedPoseGoal) -> None:
        """
        
        """
        if self.moving:
            self.preempt()

        with self.lock:
            named_poses = {}

            # TODO: clean this up...
            # Defaults to /home/qcr/.ros/configs/system_named_poses.yaml
            # config_file = self.config_path if not self.custom_configs else self.custom_configs[-1]
            config_file = '/home/qcr/armer_ws/src/armer_descriptions/data/custom/cgras_descriptions/config/named_poses.yaml'
            config_file = config_file.replace('.yaml', '_in_frame.yaml')

            try:
                config = yaml.load(open(config_file), Loader=yaml.SafeLoader)
                if config and 'named_poses' in config:
                    named_poses = config['named_poses']
            except IOError:
                rospy.logwarn(
                    'Unable to locate configuration file: {}'.format(config_file))
                self.named_pose_in_frame_server.set_aborted(
                    MoveToNamedPoseResult(success=False),
                    'Unable to locate configuration file: {}'.format(config_file)
                )
                return           

            if goal.pose_name not in named_poses:
                self.named_pose_in_frame_server.set_aborted(
                    MoveToNamedPoseResult(success=False),
                    'Unknown named pose'
                )
                rospy.logwarn(f"-- Named pose goal ({goal.pose_name}) is unknown; refusing to move...")
                return

            # TODO: YAML yuck...
            the_pose = named_poses[goal.pose_name]
            frame_id = the_pose['frame_id']
            translation = the_pose['position']
            orientation = the_pose['orientation']

            ## named PoseStamped position
            header = Header()
            header.frame_id = frame_id

            pose_stamped = PoseStamped()
            pose_stamped.header = header
  
            pose_stamped.pose.position.x = translation[0]
            pose_stamped.pose.position.y = translation[1]
            pose_stamped.pose.position.z = translation[2]

            pose_stamped.pose.orientation.w = orientation[0]
            pose_stamped.pose.orientation.x = orientation[1]
            pose_stamped.pose.orientation.y = orientation[2]
            pose_stamped.pose.orientation.z = orientation[3]

            # Transform into the current base_link ready for inv kin
            # TODO: base_link here should come from self.base_link (assuming this is base_link)
            goal_pose = self.tf_listener.transformPose(
                        f'/{self.base_link.name}',
                        pose_stamped,
                    )

            pose = goal_pose.pose

            rospy.logdebug(f"Named Pose In Frame ---\nFROM: {pose_stamped}")
            rospy.logdebug(f"Named Pose In Frame ---\nTO POSE: {goal_pose}")

            solution = None
            try:
                solution = ikine(self, target=pose, q0=self.q, end=self.gripper)
            except:
                rospy.logwarn("Failed to get IK Solution...")
                self.named_pose_in_frame_server.set_succeeded(
                  MoveToNamedPoseResult(success=False), f'Failed to solve for Named pose ({goal.pose_name}) in frame: {frame_id}'
                )
                self.executor = None
                self.moving = False
                return
            
            rospy.logdebug(f"Named Pose In Frame ---\nTO JOINTS: {solution.q.tolist()}")

            # TODO: BOB FIX THIS...
            if self.pose_within_workspace(pose) == False:
                rospy.logwarn(f"-- Named pose ({goal.pose_name}) goal outside defined workspace; refusing to move...")
                self.named_pose_in_frame_server.set_succeeded(
                  MoveToNamedPoseResult(success=False), f'Named pose ({goal.pose_name}) outside defined workspace using frame: {frame_id}'
                )
                self.executor = None
                self.moving = False
                return

            # NORMAL....            
            self.executor = TrajectoryExecutor(
                self,
                self.traj_generator(self, solution.q, goal.speed if goal.speed else 0.2),
                cutoff=self.trajectory_end_cutoff
            )

            while not self.executor.is_finished():
                rospy.sleep(0.01)

            if self.executor.is_succeeded():
                self.named_pose_in_frame_server.set_succeeded(
                        MoveToNamedPoseResult(success=True)
                )
            else:
                self.named_pose_in_frame_server.set_aborted(
                  MoveToNamedPoseResult(success=False)
                )

            self.executor = None
            self.moving = False

    def named_pose_distance_cb(self, goal: MoveToNamedPoseGoal) -> None:
        """        
        """
        # TODO: should use a custom message (speed should be max cart dist)
        proximity_limit = np.array([goal.speed]*3)

        if not goal.pose_name in self.named_poses:
            self.named_pose_distance_server.set_aborted(
                MoveToNamedPoseResult(success=False),
                'Unknown named pose'
            )

        qd = np.array(self.named_poses[goal.pose_name])
        start_SE3 = SE3(self.ets(start=self.base_link, end=self.gripper).eval(self.q))
        end_SE3 = SE3(self.ets(start=self.base_link, end=self.gripper).eval(qd))

        difference_XYZ = start_SE3.t - end_SE3.t
        rospy.logdebug(f"QD is {qd}")
        rospy.logdebug(f"Q is {self.q}")
        rospy.logdebug(f"Links are {self.base_link} (base) and {self.gripper} (gripper)")
        rospy.logdebug(f"Start is {start_SE3.t}")
        rospy.logdebug(f"End is {end_SE3.t}")
        rospy.logdebug(f"Distance to named pose {goal.pose_name} is {difference_XYZ}")
        exceeded_limit = difference_XYZ > proximity_limit
        if any(exceeded_limit):
            self.named_pose_distance_server.set_succeeded(
                    MoveToNamedPoseResult(success=True))
        else:
            self.named_pose_distance_server.set_succeeded(
                    MoveToNamedPoseResult(success=False))

    # --------------------------------------------------------------------- #
    # --------- ROS Service Callback Methods ------------------------------ #
    # --------------------------------------------------------------------- #
    def recover_cb(self, req: EmptyRequest) -> EmptyResponse: # pylint: disable=no-self-use
        """[summary]
        ROS Service callback:
        Invoke any available error recovery functions on the robot when an error occurs

        :param req: an empty request
        :type req: EmptyRequest
        :return: an empty response
        :rtype: EmptyResponse
        """
        self.general_executor(q=self.q, collision_ignore=True, workspace_ignore=True)
        rospy.loginfo(f"[RECOVER CB] -> Resetting from {self._controller_mode} state to JOINTS [Default]")
        self._controller_mode = ControlMode.JOINTS
        self.preempted = False

        return EmptyResponse()
    
    def recover_move_cb(self, req: EmptyRequest) -> EmptyResponse:
        """
        This will attempt to move the arm to a previous 'non-collision/non-singularity' state
        NOTE: uses a set moving window of 'safe states' added every step of ARMer
        """
        # Attempt recovery
        # NOTE: movement requested with collisions and workspace ingore (to recover back)
        self.general_executor(q=self.q_safe_window[0], collision_ignore=True, workspace_ignore=True)
        
        # Recover from Error State
        if self._controller_mode == ControlMode.ERROR:
            rospy.loginfo(f"[RECOVER MOVE CB] -> Resetting from ERROR state to JOINTS [Default]")
            self._controller_mode = ControlMode.JOINTS
            self.preempted = False
        else:
            rospy.logwarn(f'RECOVER MOVE CB] -> Robot [{self.name}] not in ERROR state. Do Nothing.')

        self.executor = None
        self.moving = False

        return EmptyResponse()
    
    def update_description_cb(self, req: UpdateDescriptionRequest) -> UpdateDescriptionResponse: # pylint: disable=no-self-use
        """[summary]
        ROS Service callback:
        Updates the robot description if loaded into param

        :param req: an empty request
        :type req: EmptyRequest
        :return: an empty response
        :rtype: EmptyResponse
        """
        rospy.logwarn('TF update not implemented for this arm <IN DEV>')
        rospy.loginfo(f"req gripper: {req.gripper} | param: {req.param}")
        if req.gripper == '' or req.param == '':
            rospy.logerr(f"Inputs are None or Empty")
            return UpdateDescriptionResponse(success=False)
        
        gripper_link = None
        gripper = None

        # Preempt any motion prior to changing link structure
        if self.moving:
            self.preempt()

        # Read req param and only proceed if successful
        links, _, _, _ = URDFRobot.URDF_read_description(wait=False, param=req.param)

        if np.any(links):
            #Do Something
            # Using requested gripper, update control point
            gripper = req.gripper
            gripper_link = list(filter(lambda link: link.name == gripper, links))

        # DEBUGGING
        # rospy.loginfo(f"requested gripper: {gripper} | requested gripper link: {gripper_link}")
        # rospy.loginfo(f"Updated links:")
        # for link in links:
        #     rospy.loginfo(f"{link}")

        # Update robot tree if successful
        if np.any(links) and gripper_link != []: 
            # Remove the old dict of links
            self.link_dict.clear()

            # Clear current base link
            self._base_link = None

            # Sort and update new links and gripper links
            self._sort_links(links, gripper_link, True) 

            # Update control point
            self.gripper = gripper

            # Trigger backend reset
            self.backend_reset = True

            rospy.loginfo(f"Updated Links! New Control: {self.gripper}")
            return UpdateDescriptionResponse(success=True)
        else:
            if gripper_link == []: rospy.logwarn(f"Requested control tf [{req.gripper}] not found in tree")
            if links == None: rospy.logerr(f"No links found in description. Make sure {req.param} param is correct")
            return UpdateDescriptionResponse(success=False)

    def calibrate_transform_cb(self, req: CalibrateTransformRequest) -> CalibrateTransformResponse: # pylint: disable=no-self-use
        """[summary]
        ROS Service callback:
        Attempts to calibrate the location of a link if applicable
        NOTE: links that are associated to robot joints are ignored

        :param req: a request to calibrate a link, contains the transform and link name 
        :type req: CalibrateTransformRequest
        :return: a success bool, where True is if the link was found and applicable, then set; otherwise False
        :rtype: CalibrateTransformResponse
        """
        link_found = False

        rospy.loginfo(f"Got req for transform: {req.transform} | offset: {req.link_name}")

        # Early termination on input error
        if req.link_name == None or req.transform == Transform():
            rospy.logerr(f"Input values are None or Empty")
            return CalibrateTransformResponse(success=False)
        
        # Convert transform quaternion to rpy for updating Elementary Transform Sequence (ETS) of link
        # NOTE: the order is required to set correctly
        rpy = sm.UnitQuaternion(
                req.transform.rotation.w, [
                req.transform.rotation.x,
                req.transform.rotation.y,
                req.transform.rotation.z
            ]).rpy(order='zyx')

        # Update any transforms as requested on main robot (that is not a joint)
        # NOTE: joint tf's are to be immutable (as this is assumed the robot)
        # TODO: check if parent is base link 
        for link in self.links:
            # Update if found and applicable
            if link.name == req.link_name and not link.isjoint:
                rospy.loginfo(f"LINK -> {link.name} | PARENT: {link.parent_name} | BASE: {self.base_link}")
                # NOTE: the Elementary Transform Sequence (ETS) needs the orientation
                #       in (rpy) to be applied in required order. In this case, the
                #       order is 'zyx' (see above), therefore, apply in this order
                link.ets = rtb.ET.tx(req.transform.translation.x) \
                    * rtb.ET.ty(req.transform.translation.y) \
                    * rtb.ET.tz(req.transform.translation.z) \
                    * rtb.ET.Rz(rpy[2]) \
                    * rtb.ET.Ry(rpy[1]) \
                    * rtb.ET.Rx(rpy[0]) \
                
                link_found = True
                break

        # Re-run the collision overlap dictionary update for this robot as some links may have changed
        if link_found:
            self.characterise_collision_overlaps()

        rospy.loginfo(f"Transform Calibration Pipeline Completed.")
        return CalibrateTransformResponse(success=link_found)

    def set_cartesian_impedance_cb(  # pylint: disable=no-self-use
            self,
            request: SetCartesianImpedanceRequest) -> SetCartesianImpedanceResponse:
        """ROS Service Callback
        Set the 6-DOF impedance of the end-effector. Higher values should increase the stiffness
        of the robot while lower values should increase compliance

        :param request: The numeric values representing the EE impedance (6-DOF) that should be set on the arm
        :type request: GetNamedPoseConfigsRequest
        :return: True if the impedence values were updated successfully
        :rtype: GetNamedPoseConfigsResponse
        """
        rospy.logwarn(
            'Setting cartesian impedance not implemented for this arm')
        return SetCartesianImpedanceResponse(True)
    
    def get_named_poses_cb(self, req: GetNamedPosesRequest) -> GetNamedPosesResponse:
        """
        ROS Service callback:
        Retrieves the list of named poses available to the arm

        :param req: An empty request
        :type req: GetNamesListRequest
        :return: The list of named poses available for the arm
        :rtype: GetNamesListResponse
        """
        return GetNamedPosesResponse(list(self.named_poses.keys()))
    
    def add_named_pose_in_frame_cb(self, req: AddNamedPoseInFrameRequest) -> AddNamedPoseInFrameResponse:
        """
        """
        named_poses = {}
        # Defaults to /home/qcr/.ros/configs/system_named_poses.yaml
        # config_file = self.config_path if not self.custom_configs else self.custom_configs[-1]
        config_file = '/home/qcr/armer_ws/src/armer_descriptions/data/custom/cgras_descriptions/config/named_poses.yaml'
        config_file = config_file.replace('.yaml', '_in_frame.yaml')
        try:
            config = yaml.load(open(config_file), Loader=yaml.SafeLoader)
            if config and 'named_poses' in config and config['named_poses'] != None:
                named_poses = config['named_poses']
        except IOError:
            rospy.logwarn(
                'Unable to locate configuration file: {}'.format(config_file))
            return AddNamedPoseInFrameResponse(success=False)            
           
        if req.name in named_poses and not req.overwrite:
            rospy.logerr('Named pose already exists.')
            return AddNamedPoseInFrameResponse(success=False)

        # TODO: transform into frame requested and save PoseStamped
        ## named PoseStamped position
        ee_pose = self.ets(start=self.base_link, end=self.gripper).eval(self.q.tolist())
        rospy.logerr(f'The self.base_link.name is: {self.base_link.name}')

        pose_stamped = PoseStamped()
        pose_stamped.header.frame_id = self.base_link.name

        translation = ee_pose[:3, 3]    
        pose_stamped.pose.position.x = translation[0]
        pose_stamped.pose.position.y = translation[1]
        pose_stamped.pose.position.z = translation[2]

        rotation = ee_pose[:3, :3]
        ee_rot = sm.UnitQuaternion(rotation)

        pose_stamped.pose.orientation.w = ee_rot.A[0]
        pose_stamped.pose.orientation.x = ee_rot.A[1]
        pose_stamped.pose.orientation.y = ee_rot.A[2]
        pose_stamped.pose.orientation.z = ee_rot.A[3]

        # TODO: do the transform...
        reference_frame_id = req.wrt_frame_id if req.wrt_frame_id != '' else '/base_link'
        tf = self.tf_listener.transformPose(
                    reference_frame_id,
                    pose_stamped,
                )

        # TODO: get real serialisation for PoseStamped to YAML...(use JSON!)
        yaml_posestamped = {}
        yaml_posestamped['frame_id'] = reference_frame_id
        yaml_posestamped['position'] = np.array([tf.pose.position.x, tf.pose.position.y, tf.pose.position.z]).tolist()
        yaml_posestamped['orientation'] = np.array([tf.pose.orientation.w, tf.pose.orientation.x, tf.pose.orientation.y, tf.pose.orientation.z]).tolist()

        named_poses[req.name] = yaml_posestamped

        self.__write_config('named_poses', named_poses, config_file)

        return AddNamedPoseInFrameResponse(success=True)

    def add_named_pose_cb(self, req: AddNamedPoseRequest) -> AddNamedPoseResponse:
        """
        ROS Service callback:
        Adds the current arm pose as a named pose and saves it to the host config

        :param req: The name of the pose as well as whether to overwrite if the pose already exists
        :type req: AddNamedPoseRequest
        :return: True if the named pose was written successfully otherwise false
        :rtype: AddNamedPoseResponse
        """
        if req.pose_name in self.named_poses and not req.overwrite:
            rospy.logerr('Named pose already exists.')
            return AddNamedPoseResponse(success=False)

        self.named_poses[req.pose_name] = self.q.tolist()
        config_file = self.config_path if not self.custom_configs else self.custom_configs[-1]
        self.__write_config('named_poses', self.named_poses, config_file)

        return AddNamedPoseResponse(success=True)

    def remove_named_pose_cb(self, req: RemoveNamedPoseRequest) -> RemoveNamedPoseResponse:
        """
        ROS Service callback:
        Adds the current arm pose as a named pose and saves it to the host config

        :param req: The name of the pose
        :type req: RemoveNamedPoseRequest
        :return: True if the named pose was removed successfully otherwise false
        :rtype: RemoveNamedPoseResponse
        """
        if req.pose_name not in self.named_poses:
            rospy.logerr('Named pose does not exists.')
            return RemoveNamedPoseResponse(success=False)

        del self.named_poses[req.pose_name]
        config_file = self.config_path if not self.custom_configs else self.custom_configs[-1]
        self.__write_config('named_poses', self.named_poses, config_file)

        return RemoveNamedPoseResponse(success=True)

    def export_named_pose_config_cb(
            self,
            request: NamedPoseConfigRequest) -> NamedPoseConfigResponse:
        """[summary]
        Creates a config file containing the currently loaded named_poses

        :param request: [destination]
        :type request: NamedPoseConfigRequest
        :return: [bool]
        :rtype: NamedPoseConfigRequest
        """

        # Ensure the set of named_poses is up-to-date
        self.__load_config()

        # Write to provided config_path
        self.__write_config('named_poses', self.named_poses, request.config_path)
        return True

    def add_named_pose_config_cb(
            self,
            request: NamedPoseConfigRequest) -> NamedPoseConfigResponse:
        """[summary]

        :param request: [description]
        :type request: NamedPoseConfigRequest
        :return: [description]
        :rtype: NamedPoseConfigResponse
        """
        self.custom_configs.append(request.config_path)
        self.__load_config()
        return True

    def remove_named_pose_config_cb(
            self,
            request: NamedPoseConfigRequest) -> NamedPoseConfigResponse:
        """[summary]

        :param request: [description]
        :type request: NamedPoseConfigRequest
        :return: [description]
        :rtype: [type]
        """
        if request.config_path in self.custom_configs:
            self.custom_configs.remove(request.config_path)
            self.__load_config()
        return True

    def get_named_pose_configs_cb(
            self,
            request: GetNamedPoseConfigsRequest) -> GetNamedPoseConfigsResponse:
        """[summary]

        :param request: [description]
        :type request: GetNamedPoseConfigsRequest
        :return: [description]
        :rtype: GetNamedPoseConfigsResponse
        """
        return str(self.custom_configs)
    
    def set_pid(self, msg):
        """
        Sets the pid value from a callback
        Deprecated 2023-06-19.
        """
        self.Kp = None
        self.Ki = None
        self.Kd = None

    # --------------------------------------------------------------------- #
    # --------- Collision and Singularity Checking Services --------------- #
    # --------------------------------------------------------------------- #
    def get_collision_check_window(self, req: EmptyRequest) -> EmptyResponse:
        """
        TODO: add this
        Expected input: None
        Expected output: string list of links in window
        """
        pass

    def update_collision_check_window(self, req: EmptyRequest) -> EmptyResponse:
        """
        TODO: add this
        Expected input: string name for start link, string name for stop link
        Expected output: bool success on setting
        NOTE: check failure modes (i.e., not in link dict, etc.)
        """
        pass
    
    def enable_collision_debug_cb(self, req: SetBoolRequest) -> SetBoolResponse:
        """
        Enables collision debugging via RVIZ. Displays current target shapes
        NOTE: this does slow down execution if enabled
        """
        self.collision_debug_enabled = req.data
        if self.collision_debug_enabled:
            return SetBoolResponse(success=True, message="Collision Debug is Enabled")
        else:
            return SetBoolResponse(success=True, message="Collision Debug is Disabled")

    def add_collision_obj_cb(self, req: AddCollisionObjectRequest) -> AddCollisionObjectResponse:
        """Adds a collision primative (sphere, cylinder, cuboid) to existing collision dictionary at runtime
        Expected input:
        -> name (string): to define the key within the collision dictionary
        -> type (string): to define basic primatives (as part of the sg.Shape class)
        -> TODO: base_link (string): link name to attach object to
        -> radius (float): used to define radius of sphere or cylinder
        -> length (float): used to define length of cylinder
        -> pose (Pose): to define the shape's location
        -> overwrite (bool): True means the same key name (if in the dictionary) can be overwritten

        :param req: Service request for name, type of object, radius, length or scale, as well as pose
        :type req: AddCollisionObjectRequest
        :return: Service response for success (True) or failure (False)
        :rtype: AddCollisionObjectResponse
        """
        # Handle early termination on input error for name and type
        if req.name == None or req.name == ''\
            or req.type == None or req.type == '':
            rospy.logerr(f"Add collision object service input error: name [{req.name}] | type [{req.type}]")
            return AddCollisionObjectResponse(success=False)
        
        # Handle radius and length defaulting on 0
        radius = req.radius if req.radius else 0.05 #default in m
        length = req.length if req.length else 0.1 #default in m
        scale_x = req.scale_x if req.scale_x else 0.1 #default in m
        scale_y = req.scale_y if req.scale_y else 0.1 #default in m
        scale_z = req.scale_z if req.scale_z else 0.1 #default in m
        # rospy.loginfo(f"radius is: {radius} | length is: {length}")
        
        # Handle pose input error
        if req.pose == None or req.pose == Pose():
            rospy.logerr(f"Pose is empty or None: [{req.pose}]")
            return AddCollisionObjectResponse(success=False)
        
        # Convert geometry_msgs/Pose to SE3 object
        # NOTE: only the translation component is actually used
        pose_se3 = SE3(req.pose.position.x, 
                        req.pose.position.y,
                        req.pose.position.z) * UnitQuaternion(req.pose.orientation.w, [
                                                                req.pose.orientation.x, 
                                                                req.pose.orientation.y,
                                                                req.pose.orientation.z]).SE3()
        # Handle type selection or error
        shape: sg.Shape = None
        if req.type == "sphere":
            shape = sg.Sphere(radius=radius, pose=pose_se3)
        elif req.type == "cylinder":
            shape = sg.Cylinder(radius=radius, length=length, pose=pose_se3)
        elif req.type == "cuboid" or req.type == "cube":
            shape = sg.Cuboid(scale=[scale_x, scale_y, scale_z], pose=pose_se3)
        elif req.type == "mesh":
            rospy.logwarn(f"In progress -> not yet implemented. Exiting...")
            return AddCollisionObjectResponse(success=False)
        else:
            rospy.logerr(f"Collision shape type [{req.type}] is invalid -> exiting...")
            return AddCollisionObjectResponse(success=False)
        
        # Test adding to collision dictionary for checking
        # NOTE: perform error checking on name key (unless a replace flag was set)
        interactive_marker_handle = True
        if req.name in self.collision_dict.keys() and not req.overwrite:
            rospy.logerr(f"Requested name [{req.name}] already exists in collision list and not asked to overwrite [{req.overwrite}]")
            return AddCollisionObjectResponse(success=False)
        elif req.name in self.collision_dict.keys() and req.overwrite:
            # TODO: remove previous dynamic object with current key from dynamic collision list
            #       done by retrieving object with matching name and setting removal_requested to True
            rospy.logwarn(f"[NOT YET FULLY IMPLEMENTED] Here attempting to remove existing shape")
            removed_obj = self.dynamic_collision_dict.pop(req.name)
            self.dynamic_collision_removal_dict[req.name] = removed_obj
            interactive_marker_handle = False
    
        # Create a Dynamic Collision Object and add the configured shape to the scene
        dynamic_obj = DynamicCollisionObj(shape=shape, key=req.name, pose=req.pose, is_added=False)
        # NOTE: these dictionaries are accessed and checked by Armer's main loop for addition to a backend.
        with self.lock:
            self.dynamic_collision_dict[req.name] = dynamic_obj
            self.collision_dict[req.name] = [dynamic_obj.shape]

        # Add to list of objects for NEO
        # NOTE: NEO needs more work to avoid local minima
        # NOTE: Also, existing description shapes (not part of the robot tree) need to be peeled out and added here as well
        self.collision_obj_list.append(shape)

        # Re-update the collision overlaps on insertion
        # Needed so links expected to be in collision with shape are correctly captured
        self.characterise_collision_overlaps()
        
        # print(f"Current collision objects: {self.collision_obj_list}")
        # Add the shape as an interactive marker for easy re-updating
        # NOTE: handle to not add if overwrite requested
        # NOTE: wait for shape to be added to backend
        if interactive_marker_handle:
            rospy.sleep(rospy.Duration(secs=0.5))
            self.interactive_marker_creation()

        return AddCollisionObjectResponse(success=True)
    
    def remove_collision_obj_cb(self, req: RemoveCollisionObjectRequest) -> RemoveCollisionObjectResponse:
        """This will take a given key and (if it exists) and removes said key object as a collision shape
        NOTE: currently expects the following
        -> name (string) to access object in question
        """
        # Handle early termination on input error for name and type
        if req.name == None or req.name == '':
            rospy.logerr(f"[REMOVE COLLISION OBJ CB] -> Remove collision object service input error: name [{req.name}] | type [{req.type}]")
            return RemoveCollisionObjectResponse(success=False)
        
        if req.name in self.collision_dict.keys(): 
            # Stage Backend to Remove Object
            # NOTE: swift backend issue will not visually remove (but does correctly remove)
            removed_obj = self.dynamic_collision_dict.pop(req.name)
            self.dynamic_collision_removal_dict[req.name] = removed_obj

            # Remove from Collision Dict
            self.collision_dict.pop(req.name)

            # Re-characterise overlaps after removal
            self.characterise_collision_overlaps()

            # Remove interactive marker 
            self.interactive_marker_server.erase(name=req.name)
            self.interactive_marker_server.applyChanges()

            return RemoveCollisionObjectResponse(success=True)
        else:
            rospy.logerr(f"[REMOVE COLLISION OBJ CB] -> Unknown name [{req.name}] requested; not in collision dictionary")
    
    def get_collision_obj_cb(self, req: GetCollisionObjectsRequest) -> GetCollisionObjectsResponse:
        """
        This will return a list (string) of current dynamic collision object names
        
        TODO: could have more information displayed? The translation only is displayed. Can do rpy (but escape chars need handling)
        """
        out_list = list()
        for key, value in self.dynamic_collision_dict.items():
            out_list.append(str(key) + f" -> (shape: {value.shape.stype}, pose (x,y,z): {str(sm.SE3(value.shape.T).t)})")

        # Dump list out
        # return GetCollisionObjectsResponse(list(self.dynamic_collision_dict.keys()))
        return GetCollisionObjectsResponse(out_list)
    
    def save_collision_config_cb(self, req: CollisionSceneConfigRequest) -> CollisionSceneConfigResponse:
        """Attempts to save the current dynamic collision object dictionary

        :param req: A service request with a config_path (if empty, defaults to initialised path)
        :type req: CollisionSceneConfigRequest
        :return: True on Success or False
        :rtype: CollisionSceneConfigResponse
        """
        if self.write_collision_scene_config(config_path=req.config_path):
            return CollisionSceneConfigResponse(success=True)
        else:
            return CollisionSceneConfigResponse(success=False)

    def load_collision_config_path_cb(self, req: CollisionSceneConfigRequest) -> CollisionSceneConfigResponse:
        """Attempts to load a collision scene config from a given path

        :param req: Service containing a request for the 'config_path'
        :type req: AddCollisionSceneConfigRequest
        :return: Service containing a response to the activity 'bool' on success or failure
        :rtype: AddCollisionSceneConfigResponse
        """
        if self.load_collision_scene_config(config_path=req.config_path):
            return CollisionSceneConfigResponse(success=True)
        else:
            return CollisionSceneConfigResponse(success=False)
    
    # --------------------------------------------------------------------- #
    # --------- Collision and Singularity Checking Methods ---------------- #
    # --------------------------------------------------------------------- #
    def add_collision_obj(self, obj):
        """
        Simple mechanism for adding a shape object to the collision list
        NOTE: only for debugging purposes
        """
        self.collision_obj_list.append(obj)

    def closest_dist_query_shape_based(self, sliced_link_name, target_links):
        """
        This method uses the closest point method of a Shape object to extract translation to a link.
        NOTE: only a single shape is used per link (defaulting to the first one). This is needed to
        keep the speed of this process as fast as possible. This may not be the best method if the 
        shape being used is not 'representative' of the entire link.
        """
        translation_dict = {
            link: self.collision_dict[sliced_link_name][0].closest_point(self.collision_dict[link][0])[2]
            for link in target_links 
            if self.collision_dict[link] != [] and link not in self.overlapped_link_dict[sliced_link_name]
        }
        return translation_dict
    
    def closest_dist_query_pose_based(self, sliced_link_name, magnitude_thresh: float = 0.4, refine: bool = False):
        """
        This method uses the built-in recursive search (ets) for links within the robot tree
        to quickly get the translation from a specified sliced link. Note that this method
        gets the translation to the link's origin, which may not be as representative as possibe with
        respect to the surface of the link.
        NOTE: an additional external dictionary is needed for dynamically added shapes, or shapes/links not
        within the robot tree (fails at the ets method)
        NOTE: currently the fastest method as of 2023-12-1
        """
        col_dict_cp = self.collision_dict.copy()
        # If asked to refine, then base the link dictionary creation on the magnitude threshold value (m)
        # NOTE: refining can lead to slow down on large number of multiple shapes
        if refine:
            translation_dict = {
                link: self.ets(start=sliced_link_name, end=link).eval(self.q)[:3, 3]
                for link in col_dict_cp.keys()
                if link in self.link_dict.keys() 
                    and col_dict_cp[link] != [] 
                    and link not in self.overlapped_link_dict[sliced_link_name]
                    and link != sliced_link_name
                    and np.linalg.norm(
                        self.ets(start=sliced_link_name, end=link).eval(self.q)[:3, 3]
                    ) < magnitude_thresh
            }

            # This is the external objects translation from the base_link
            external_dict = {
                link: col_dict_cp[link][0].T[:3,3]
                for link in col_dict_cp.keys()
                if link not in self.link_dict.keys()
                    and col_dict_cp[link] != []
                    and link not in self.overlapped_link_dict[sliced_link_name]
                    and link != sliced_link_name
                    and np.linalg.norm(
                        math.dist(
                            self.ets(start=self.base_link.name, end=sliced_link_name).eval(self.q)[:3, 3],
                            col_dict_cp[link][0].T[:3,3]
                        )
                    ) < magnitude_thresh
            }
        else:
            translation_dict = {
                link: self.ets(start=sliced_link_name, end=link).eval(self.q)[:3, 3]
                for link in col_dict_cp.keys()
                if link in self.link_dict.keys() 
                    and col_dict_cp[link] != [] 
                    and link not in self.overlapped_link_dict[sliced_link_name]
                    and link != sliced_link_name
            }

            # This is the external objects translation from the base_link
            external_dict = {
                link: col_dict_cp[link][0].T[:3,3]
                for link in col_dict_cp.keys()
                if link not in self.link_dict.keys()
                    and col_dict_cp[link] != []
                    and link not in self.overlapped_link_dict[sliced_link_name]
                    and link != sliced_link_name
            }

        translation_dict.update(external_dict)
        return translation_dict
    
    def collision_marker_debugger(self, sliced_link_names: list = [], check_link_names: list = []):
        """
        A simple debugging method to output to RVIZ the current link shapes being checked
        """
        marker_array = []
        captured = []
        counter = 0
        for links in check_link_names:
            # These are the links associated with the respective idx sliced link
            for link in links:
                # Check if we have already created a marker
                if link in captured:
                    continue

                # Get all the shape objects and create a marker for each
                for shape in self.collision_dict[link]:
                    # Default setup of marker header
                    marker = Marker()

                    # NOTE: this is currently an assumption as dynamic objects as easily added with respect to base link
                    # TODO: add with respect to any frame would be good
                    # NOTE: mesh objects not working (need relative pathing, not absolute)
                    if link not in self.link_dict.keys():
                        marker.header.frame_id = self.base_link.name
                    else:
                        marker.header.frame_id = link

                    marker.header.stamp = rospy.Time.now()
                    if shape.stype == 'sphere':
                        marker.type = 2
                        # Expects diameter (m)
                        marker.scale.x = shape.radius * 2
                        marker.scale.y = shape.radius * 2
                        marker.scale.z = shape.radius * 2
                    elif shape.stype == 'cylinder':
                        marker.type = 3

                        # Expects diameter (m)
                        marker.scale.x = shape.radius * 2
                        marker.scale.y = shape.radius * 2
                        marker.scale.z = shape.length
                    elif shape.stype == 'cuboid':
                        marker.type = 1

                        # Expects diameter (m)
                        marker.scale.x = shape.scale[0]
                        marker.scale.y = shape.scale[1]
                        marker.scale.z = shape.scale[2]
                    else:
                        break
                
                    marker.id = counter
                    pose_se3 = sm.SE3(shape.T)
                    uq = UnitQuaternion(pose_se3)
                    marker.pose.orientation = Quaternion(*np.concatenate([uq.vec3, [uq.s]]))
                    marker.pose.position = Point(*pose_se3.t)

                    if link in sliced_link_names:
                        marker.color.r = 0.5
                        marker.color.g = 0.5
                    else:
                        marker.color.r = 0
                        marker.color.g = 1
                    
                    marker.color.b = 0
                    marker.color.a = 0.25
                    counter+=1

                    marker_array.append(marker)
            
                captured.append(link)

        # Publish array of markers
        self.collision_debug_publisher.publish(marker_array)
    
    def query_target_link_check(self, sliced_link_name: str = "", magnitude_thresh: float = 0.2):
        """
        This method is intended to run per cycle of operation and is expected to find changes to distances
        based on the original main dictionary (as created by creation_of_pose_link_distances)
        """
        col_dict_cp = self.collision_dict.copy()
        target_list = [
            link
            for link in col_dict_cp.keys()
            if link in self.link_dict.keys() 
                and col_dict_cp[link] != [] 
                and link not in self.overlapped_link_dict[sliced_link_name]
                and link != sliced_link_name
                and np.linalg.norm(
                    self.ets(start=sliced_link_name, end=link).eval(self.q)[:3, 3]
                ) < magnitude_thresh
        ]

        external_list =[
            link
            for link in col_dict_cp.keys()
            if link not in self.link_dict.keys()
                and col_dict_cp[link] != []
                and link not in self.overlapped_link_dict[sliced_link_name]
                and link != sliced_link_name
                and np.linalg.norm(
                    math.dist(
                        self.ets(start=self.base_link.name, end=sliced_link_name).eval(self.q)[:3, 3],
                        col_dict_cp[link][0].T[:3,3]
                    )
                ) < magnitude_thresh
        ]

        return target_list + external_list

    def query_kd_nn_collision_tree(self, sliced_links: list = [], dim: int = 4) -> list:
        """
        Given a list of links (sliced), this method returns nearest neighbor links for collision checking
        Aims to improve efficiency by identifying dim closest objects for collision checking per link
        """
        # Early termination
        if sliced_links == None or sliced_links == []:
            rospy.logerr(f"[COLLISION KD TREE QUERY] -> target links: {sliced_links} is not valid. Exiting...")
            return []

        # print(f"cylinder link check: {self.link_dict['cylinder_link']}")
        # Iterate through each sliced link (target link) 
        # For the current sliced link; find the closest point between one of the link's shapes
        # NOTE: each link can have multiple shapes, but in the first instance, we take only one shape per link to 
        #       understand the distance to then calculate the target links via the KDTree
        check_links = []
        for sliced_link in sliced_links:
            # Early termination on error
            if sliced_link.name not in self.link_dict.keys():
                rospy.logerr(f"[COLLISION KD TREE QUERY] -> Given sliced link: {sliced_link.name} is not valid. Skipping...")
                continue

            # Testing refinement using link poses (Individual method needed for shape-based version)
            # start = timeit.default_timer()
            # target_link_list = self.query_target_link_check(sliced_link_name=sliced_link.name, magnitude_tresh=0.4)
            # end = timeit.default_timer()
            # print(f"[Link Pose Based] Extraction of Target Links for Surface Check: {1/(end-start)} hz")

            # Initial approach to get closest link (based on link origin, not surface)
            # NOTE: as it is based on origin, the size/shape of collision object matters
            # NOTE: fastest method as of 2023-12-1
            start = timeit.default_timer()
            translation_dict = self.closest_dist_query_pose_based(sliced_link_name=sliced_link.name)
            end = timeit.default_timer()
            # print(f"[OLD] Get distances to links from target: {1/(end-start)} hz")
         
            tree = KDTree(data=list(translation_dict.copy().values()))
            target_position = self.ets(start=self.base_link, end=sliced_link).eval(self.q)[:3, 3]
            # Test query of nearest neighbors for a specific shape (3D) as origin (given tree is from source)
            dist, ind = tree.query(X=[target_position], k=len(translation_dict.keys()) if len(translation_dict.keys()) < dim else dim, dualtree=True)
            # print(f"dist: {dist} | links: {[list(translation_dict.keys())[i] for i in ind[0]]} | ind[0]: {ind[0]}")

            check_links.append([list(translation_dict.keys())[i] for i in ind[0]])

        return check_links

    def trajectory_collision_checker(self, traj) -> bool:
        """
        - Checks against a given trajectory (per state) if a collision is found
        - Outputs a debugging marker trail of path for visualisation
        
        Returns True valid (no collisions), otherwise False if found
        """
        # Attempt to slice the trajectory into bit-size chuncks to speed up collision check
        # NOTE: doesn't need to check every state in trajectory, as spatially stepping will find links in collision
        # NOTE: the smaller the step thresh is, the less points used for checking (resulting in faster execution)
        # TODO: check this assumption holds true
        step_thresh = 10
        step_value = int(len(traj.s) / step_thresh) if int(len(traj.s) / step_thresh) > 0 else 1
        
        # Create marker array for representing trajectory
        marker_traj = Marker()
        marker_traj.header.frame_id = self.base_link.name
        marker_traj.header.stamp = rospy.Time.now()
        marker_traj.action = Marker.ADD
        marker_traj.pose.orientation.w = 1
        marker_traj.id = 0
        marker_traj.type = Marker.LINE_STRIP
        marker_traj.scale.x = 0.01
        # Default to Green Markers (Updated if in Collision)
        marker_traj.color.g = 1.0
        marker_traj.color.a = 1.0

        # Initial empty publish
        marker_traj.points = []
        self.display_traj_publisher.publish(marker_traj)

        rospy.loginfo(f"[TRAJECTORY COLLISION CHECK] -> Traj len: {len(traj.s)} | with step value: {step_value}")
        go = True

        # Quick end state check prior to full trajectory check (in steps)
        # NOTE: the rest of the trajectory is still parsed to display in RVIZ for debugging
        if self.check_collision_per_state(q=traj.s[-1]) == False:
            go=False
            # Update colour to RED for error
            marker_traj.color.g = 0.0
            marker_traj.color.r = 1.0
  
        # Goal state is clear, check rest of trajectory in steps
        with Timer("Full Trajectory Collision Check", enabled=True):
            for idx in range(0,len(traj.s),step_value):
                
                # Calculate end-effector pose and extract translation component
                pose = self.ets(start=self.base_link, end=self.gripper).eval(traj.s[idx])
                extracted_t = pose[:3, 3]

                # Update marker trajectory for visual representation
                p = Point()
                p.x = extracted_t[0]
                p.y = extracted_t[1]
                p.z = extracted_t[2]
                marker_traj.points.append(p)

                if self.check_collision_per_state(q=traj.s[idx]) == False:
                    # Terminate on collision check failure
                    # NOTE: currently terminates on collision for speed and efficiency
                    #       could continue (regardless) to show 'full' trajectory with collision
                    #       component highlighted? Note, that speed is a function of 
                    #       step_thresh (currently yields approx. 30Hz on calculation 
                    #       for a 500 sample size traj at a step_tresh of 10)
                    go=False
                    # Update colour to RED for error
                    marker_traj.color.g = 0.0
                    marker_traj.color.r = 1.0
                    break

        # Publish marker (regardless of failure case for visual identification)
        self.display_traj_publisher.publish(marker_traj)
        # Output go based on passing collision check
        return go
    
    def get_link_collision_dict(self) -> dict:
        """
        Returns a dictionary of all associated links (names) which lists their respective collision data
        To be used by high-level armer class for collision handling
        """
        return self.collision_dict
    
    def check_collision_per_state(self, q: list = []) -> bool:
        """
        Given a robot state (q) this method checks if the links (of a ghost robot) will result in a collision
        If a collision is found, then the output is True, else False
        """
        with Timer(name="setup backend and state", enabled=False):    
            env = self.robot_ghost._get_graphical_backend()
            self.robot_ghost.q = q
            env.launch(headless=True)
            env.add(self.robot_ghost, readonly=True)
                
            go_signal = True

        with Timer(f"Get collision per state:", enabled=False):
            # NOTE: use current (non-ghost) robot's link names to extract ghost robot's link from dictionary
            for link in self.collision_dict.keys():
                # Get the link to check against from the ghost robot's link dictionary
                # Output as a list of names
                links_in_collision = self.get_links_in_collision(
                    target_link=link, 
                    check_list=self.robot_ghost.link_dict[link].collision.data if link in self.robot_ghost.link_dict.keys() else self.collision_dict[link], 
                    ignore_list=self.overlapped_link_dict[link] if link in self.overlapped_link_dict.keys() else [],
                    link_list=self.robot_ghost.links,
                    output_name_list=True,
                    skip=True)
            
                # print(f"Checking [{link}] -> links in collision: {links_in_collision}")
                
                if len(links_in_collision) > 0:
                    rospy.logerr(f"[COLLISION PER STATE CHECK] -> Collision in trajectory between -> [{link}] and {[link_n for link_n in links_in_collision]}")
                    go_signal = False
                    break

            return go_signal 
    
    def update_link_collision_window(self):
        """This method updates a sliced list of links (member variable)
        as determined by the class method variables:
        - collision_check_start_link
        - collision_check_stop_link
        """
        with Timer("Link Slicing Check", enabled=False):
            # Prepare sliced link based on a defined stop link 
            # TODO: this could be update-able for interesting collision checks based on runtime requirements
            # NOTE: the assumption here is that each link is unique (which is handled low level by rtb) so we take the first element if found
            # NOTE: sorted links is from base link upwards to end-effector. We want to slice from stop link to start in rising index order
            col_start_link_idx = [i for i, link in enumerate(self.sorted_links) if link.name == self.collision_check_start_link]
            col_stop_link_idx = [i for i, link in enumerate(self.sorted_links) if link.name == self.collision_check_stop_link]
            # print(f"start_idx: {col_start_link_idx} | stop_idx: {col_stop_link_idx}")

            # NOTE: slice indexes are lists, so confirm data inside
            if len(col_start_link_idx) > 0 and len(col_stop_link_idx) > 0:
                start_idx = col_start_link_idx[0]
                end_idx = col_stop_link_idx[0]

                # Terminate early on invalid indexes
                if start_idx < end_idx or start_idx > len(self.sorted_links):
                    rospy.logwarn(f"[COLLISION SLICE WINDOW SETUP] -> Start and End idx are incompatible, defaulting to full link list")
                    return 

                # Handle end point
                if start_idx == len(self.sorted_links):
                    self.collision_sliced_links = self.sorted_links[end_idx:None]
                else:
                    self.collision_sliced_links = self.sorted_links[end_idx:start_idx + 1]

                # Reverse order for sorting from start to end
                self.collision_sliced_links.reverse()    

                rospy.loginfo(f"[COLLISION SLICE WINDOW SETUP] -> Window Set: {[link.name for link in self.collision_sliced_links]}")
            else:
                # Defaul to the current list of sorted links (full)
                self.collision_sliced_links = self.sorted_links
                self.collision_sliced_links.reverse()

    def characterise_collision_overlaps(self, debug: bool = False) -> bool:
        """Characterises the existing robot tree and tracks overlapped links in collision handling
        NOTE: needed to do collision checking, when joints are typically (neighboring) overlapped
        NOTE: this is quite an intensive run at the moment, however, it is only expected to be run in single intervals (not continuous)
        - [2023-10-27] approx. time frequency is 1hz (Panda simulated)
        - [2023-10-31] approx. time frequency is 40Hz and 21Hz (UR10 and Panda simulated with better method, respectively)

        :param debug: True to output debugging information on collision overlaps, defaults to False
        :type debug: bool, optional
        :return: True if successfully completed or False if in error
        :rtype: bool
        """
        # Running timer to get frequency of run. Set enabled to True for debugging output to stdout
        with Timer(name="Characterise Collision Overlaps", enabled=True):
            # Error handling on gripper name
            if self.gripper == None or self.gripper == "":
                rospy.logerr(f"[OVERLAP COLLISION CHARACTERISE] -> gripper name is invalid: {self.gripper}")
                return False 
            
            # Error handling on empty lick dictionary (should never happen but just in case)
            if self.link_dict == dict() or self.link_dict == None:
                rospy.logerr(f"[OVERLAP COLLISION CHARACTERISE] -> link dictionary is invalid: {self.link_dict}")
                return False
            
            # Error handling on collision object dict
            if self.collision_dict == dict() or self.collision_dict == None:
                rospy.logerr(f"[OVERLAP COLLISION CHARACTERISE] -> collision dictionary is invalid: [{self.collision_dict}]")
                return False
            
            # Alternative Method (METHOD 2) that is getting the list in a faster iterative method
            # NOTE: this has to course through ALL links in space (self.links encapsulates all links that are not the gripper)
            self.overlapped_link_dict = dict([
                (link, self.get_links_in_collision(
                    target_link=link, 
                    check_list=self.collision_dict[link], 
                    ignore_list=[],
                    link_list=self.links,
                    output_name_list=True)
                )
                for link in self.collision_dict.keys()])
            
            # NOTE: secondary run to get gripper links as well
            gripper_dict = dict([
                (link.name, self.get_links_in_collision(
                    target_link=link.name, 
                    check_list=self.collision_dict[link.name], 
                    ignore_list=[],
                    output_name_list=True)
                )
                for link in reversed(self.grippers)])
            
            self.overlapped_link_dict.update(gripper_dict)

            # using json.dumps() to Pretty Print O(n) time complexity
            if debug:
                rospy.loginfo(f"[OVERLAP COLLISION CHARACTERISE] -> Collision Overlaps per link: {json.dumps(self.overlapped_link_dict, indent=4)}")

        # Reached end in success
        return True
    
    def get_links_in_collision(self, target_link: str, 
                               ignore_list: list = [], 
                               check_list: list = [], 
                               link_list: list = [], 
                               output_name_list: bool = False,
                               skip: bool = True):
        """
        An alternative method that returns a list of links in collision with target link.
        NOTE: ignore list used to ignore known overlapped collisions (i.e., neighboring link collisions)
        NOTE: check_list is a list of Shape objects to check against.
        """
        with Timer("NEW Get Link Collision", enabled=False):
            # rospy.loginfo(f"Target link requested is: {target_link}")
            if link_list == []:
                link_list = self.sorted_links
            
            # Handle invalid link name input
            if target_link == '' or target_link == None or not isinstance(target_link, str):
                rospy.logwarn(f"[GET LINKS IN COLLISION] -> Link name [{target_link}] is invalid.")
                return []
            
            # Handle check list empty scenario
            if check_list == []:
                # print(f"Check list is empty so terminate.")
                return []

            # DEBUGGING
            # rospy.loginfo(f"{target_link} has the following collision objects: {check_list}")
            
            # NOTE iterates over all configured links and compares against provided list of check shapes 
            # NOTE: the less objects in a check list, the better
            #       this is to handle cases (like with the panda) that has multiple shapes per link defining its collision geometry
            #       any custom descriptions should aim to limit the geometry per link as robot geometry is controlled by the vendor
            # NOTE: ignore list is initiased at start up and is meant to handle cases where a mounted table (in collision with the base) is ignored
            #       i.e., does not throw a collision for base_link in collision with table (as it is to be ignored) but will trigger for end-effector link
            check_dict = dict([(link.name, link) \
                for obj in check_list \
                for link in reversed(link_list) \
                if (link.name not in ignore_list) and (link.name != target_link) and (link.iscollided(obj, skip=skip))
            ])
            
            # print(f"links: {[link.name for link in self.links]}")
            # print(f"Collision Keys: {list(check_dict.keys())}") if len(check_dict.keys()) > 0 else None   
            # print(f"Collision Values: {list(check_dict.values())}")    

            # Output list of collisions or name of links based on input bool
            if output_name_list:
                return list(check_dict.keys())
            else:
                return list(check_dict.values())
    
    def check_link_collision(self, target_link: str, sliced_links: list = [], ignore_list: list = [], check_list: list = []):
        """
        This method is similar to roboticstoolbox.robot.Robot.iscollided
        NOTE: ignore list used to ignore known overlapped collisions (i.e., neighboring link collisions)
        NOTE: archived for main usage, but available for one shot checks if needed
        """
        with Timer(name="OLD Check Link Collision", enabled=False):
            rospy.loginfo(f"Target link requested is: {target_link}")
            # Handle invalid link name input
            if target_link == '' or target_link == None or not isinstance(target_link, str):
                rospy.logwarn(f"Self Collision Check -> Link name [{target_link}] is invalid.")
                return None, False
            
            # Handle check list empty scenario
            if check_list == []:
                # print(f"Check list is empty so terminate.")
                return None, False

            # DEBUGGING
            # rospy.loginfo(f"{target_link} has the following collision objects: {check_list}")
            
            # NOTE iterates over all configured links and compares against provided list of check shapes 
            # NOTE: the less objects in a check list, the better
            #       this is to handle cases (like with the panda) that has multiple shapes per link defining its collision geometry
            #       any custom descriptions should aim to limit the geometry per link as robot geometry is controlled by the vendor
            # NOTE: ignore list is initiased at start up and is meant to handle cases where a mounted table (in collision with the base) is ignored
            #       i.e., does not throw a collision for base_link in collision with table (as it is to be ignored) but will trigger for end-effector link
            for link in reversed(sliced_links):
                # print(f"Link being checked: {link.name}")
                # Check against ignore list and continue if inside
                # NOTE: this assumes that the provided target link (dictating the ignore list) is unique
                #       in some cases the robot's links (if multiple are being checked) may have the same named links
                #       TODO: uncertain if this is scalable (currently working on two pandas with the same link names), but check this
                # NOTE: ignore any links that are expected to be overlapped with current link (inside current robot object)
                if link.name in ignore_list: 
                    # print(f"{link.name} is in list: {ignore_list}, so skipping")
                    continue

                # Ignore check if the target link is the same
                if link.name == target_link:
                    # rospy.logwarn(f"Self Collision Check -> Skipping the current target: {link.name}")
                    continue

                # NOTE: as per note above, ideally this loop should be a oneshot (in most instances)
                # TODO: does it make sense to only check the largest shape in this list? 
                for obj in check_list:
                    # rospy.logwarn(f"LOCAL CHECK for [{self.name}] -> Checking: {link.name}")
                    if link.iscollided(obj, skip=True):
                        rospy.logerr(f"Self Collision Check -> Link that is collided: {link.name}")
                        return link, True
                
            return None, False

    def neo(self, Tep, velocities):
        """
        Runs a version of Jesse H.'s NEO controller
        <IN DEVELOPMENT>
        """
        ##### Determine Slack #####
        # Transform from the end-effector to desired pose
        Te = self.fkine(self.q)
        eTep = Te.inv() * Tep
        # Spatial error
        e = np.sum(np.abs(np.r_[eTep.t, eTep.rpy() * np.pi / 180]))

        # Gain term (lambda) for control minimisation
        Y = 0.01

        # Quadratic component of objective function
        Q = np.eye(len(self.q) + 6)

        # Joint velocity component of Q
        Q[:len(self.q), :len(self.q)] *= Y

        # Slack component of Q
        Q[len(self.q):, len(self.q):] = (1 / e) * np.eye(6)

        ##### Determine the equality/inequality constraints #####
        # The equality contraints
        Aeq = np.c_[self.jacobe(self.q), np.eye(6)]
        beq = velocities.reshape((6,))

        # The inequality constraints for joint limit avoidance
        Ain = np.zeros((len(self.q) + 6, len(self.q) + 6))
        bin = np.zeros(len(self.q) + 6)

        # The minimum angle (in radians) in which the joint is allowed to approach
        # to its limit
        ps = 0.05

        # The influence angle (in radians) in which the velocity damper
        # becomes active
        pi = 0.9

        # Form the joint limit velocity damper
        Ain[:len(self.q), :len(self.q)], bin[:len(self.q)] = self.joint_velocity_damper(ps, pi, len(self.q))

        ###### TODO: look for collision objects and form velocity damper constraints #####
        for collision in self.collision_obj_list:
            # print(f"collision obj: {collision}")
            # Form the velocity damper inequality contraint for each collision
            # object on the robot to the collision in the scene
            c_Ain, c_bin = self.link_collision_damper(
                collision,
                self.q[:len(self.q)],
                0.3,
                0.05,
                1.0,
                start=self.link_dict["panda_link1"],
                end=self.link_dict["panda_hand"],
            )

            # print(f"c_Ain: {np.shape(c_Ain)} | Ain: {np.shape(Ain)}")
            # If there are any parts of the robot within the influence distance
            # to the collision in the scene
            if c_Ain is not None and c_bin is not None:
                c_Ain = np.c_[c_Ain, np.zeros((c_Ain.shape[0], 4))]

                # print(f"c_Ain (in prob area): {np.shape(c_Ain)} | Ain: {np.shape(Ain)}")
                # Stack the inequality constraints
                Ain = np.r_[Ain, c_Ain]
                bin = np.r_[bin, c_bin]

        # Linear component of objective function: the manipulability Jacobian
        c = np.r_[-self.jacobm(self.q).reshape((len(self.q),)), np.zeros(6)]

        # The lower and upper bounds on the joint velocity and slack variable
        if np.any(self.qdlim):
            lb = -np.r_[self.qdlim[:len(self.q)], 10 * np.ones(6)]
            ub = np.r_[self.qdlim[:len(self.q)], 10 * np.ones(6)]

            # Solve for the joint velocities dq
            qd = qp.solve_qp(Q, c, Ain, bin, Aeq, beq, lb=lb, ub=ub, solver='daqp')
        else:
            qd = None

        return qd

    def check_singularity(self, q=None) -> bool:
        """
        Checks the manipulability as a scalar manipulability index
        for the robot at the joint configuration to indicate singularity approach. 
        - It indicates dexterity (how well conditioned the robot is for motion)
        - Value approaches 0 if robot is at singularity
        - Returns True if close to singularity (based on threshold) or False otherwise
        - See rtb.robots.Robot.py for details

        :param q: The robot state to check for manipulability.
        :type q: numpy array of joints (float)
        :return: True (if within singularity) or False (otherwise)
        :rtype: bool
        """
        # Get the robot state manipulability
        self.manip_scalar = self.manipulability(q)

        # Debugging
        # rospy.loginfo(f"Manipulability: {manip_scalar} | --> 0 is singularity")

        if (np.fabs(self.manip_scalar) <= self.singularity_thresh and self.preempted == False):
            self.singularity_approached = True
            return True
        else:
            self.singularity_approached = False
            return False

    def check_collision(self) -> bool:
        """High-level check of collision
        NOTE: this is called by loop to verify preempt of robot
        NOTE: This may not be needed as main armer class (high-level) will check collisions per robot

        :return: True if Collision is Found, else False
        :rtype: bool
        """
        # Check for collisions
        # NOTE: optimise this as much as possible
        # NOTE: enabled in below Timer line to True for debugging print of frequency of operation
        with Timer(name="Collision Check",enabled=False):
            collision = self.full_collision_check()

        # Handle checking
        if collision and self.preempted == False:
            self.collision_approached = True
            return True
        else:
            self.collision_approached = False
            return False
        
    def set_safe_state(self):
        """Updates a moving window of safe states 
        NOTE: only updated when arm is not in collision 
        """
        # Account for pointer location
        if self.q_safe_window_p < len(self.q_safe_window):
            # Add current target bar x value
            self.q_safe_window[self.q_safe_window_p] = self.q
            # increment pointer(s)
            self.q_safe_window_p += 1
        else:
            # shift all values to left by 1 (defaults to 0 at end)
            self.q_safe_window = np.roll(self.q_safe_window, shift=-1, axis=0)
            # add value to end
            self.q_safe_window[-1] = self.q  

    def load_collision_scene_config(self, config_path: str = ''):
        """Attempts to load in a collision scene from a config
        """
        if config_path == '' or config_path == None:
            rospy.logwarn(f"[LOAD COLLISION SCENE] -> Provided path is invalid. Defaulting to [{self.collision_scene_default_path}]")
            config_path = self.collision_scene_default_path

        # Check if the path exists (initialised from ARMer) and loads data
        if os.path.exists(config_path):
            try:
                config = yaml.load(open(config_path),
                                   Loader=yaml.SafeLoader)
                if config:
                    for key, value in config.items():
                        # Convert geometry_msgs/Pose and SE3 object
                        pose = Pose()
                        pose.position.x = value['pos_x']
                        pose.position.y = value['pos_y']
                        pose.position.z = value['pos_z']
                        pose.orientation.w = value['rot_w']
                        pose.orientation.x = value['rot_x']
                        pose.orientation.y = value['rot_y']
                        pose.orientation.z = value['rot_z']

                        pose_se3 = SE3(value['pos_x'], 
                                        value['pos_y'],
                                        value['pos_z']) * UnitQuaternion(value['rot_w'], [
                                                                                value['rot_x'], 
                                                                                value['rot_y'],
                                                                                value['rot_z']]).SE3()
                        # Handle type selection or error
                        shape: sg.Shape = None
                        if value['shape'] == "sphere":
                            shape = sg.Sphere(radius=value['radius'], pose=pose_se3)
                        elif value['shape'] == "cylinder":
                            shape = sg.Cylinder(radius=value['radius'], length=value['length'], pose=pose_se3)
                        elif value['shape'] == "cuboid" or value['shape'] == "cube":
                            shape = sg.Cuboid(scale=[value['scale_x'], value['scale_y'], value['scale_z']], pose=pose_se3)
                        elif value['shape'] == "mesh":
                            rospy.logwarn(f"In progress -> not yet implemented. Exiting...")
                            break
                        else:
                            rospy.logerr(f"Collision shape type [{value['shape']}] is invalid -> exiting...")
                            break
                        
                        # Create a Dynamic Collision Object and add the configured shape to the scene
                        dynamic_obj = DynamicCollisionObj(shape=shape, key=key, pose=pose, is_added=False)
                        # NOTE: these dictionaries are accessed and checked by Armer's main loop for addition to a backend.
                        with self.lock:
                            self.dynamic_collision_dict[key] = dynamic_obj
                            self.collision_dict[key] = [dynamic_obj.shape]

                        # Add to list of objects for NEO
                        self.collision_obj_list.append(dynamic_obj.shape)

                    # Re-update the collision overlaps on insertion
                    # Needed so links expected to be in collision with shape are correctly captured
                    self.characterise_collision_overlaps()
    
                    # print(f"Current collision objects: {self.collision_obj_list}")
                    # Add the shape as an interactive marker for easy re-updating
                    # NOTE: handle to not add if overwrite requested
                    # NOTE: wait for shape to be added to backend
                    rospy.sleep(rospy.Duration(secs=0.5))
                    self.interactive_marker_creation()
                    rospy.loginfo(f"[LOAD COLLISION SCENE] -> Scene Loaded Successfully")
                else:
                    rospy.logwarn(f"[LOAD COLLISION SCENE] -> Nothing to Load for Collision Scene...")
            except IOError:
                pass
        else:
            rospy.logerr(f"[LOAD COLLISION SCENE] -> Provided Path [{self.collision_scene_default_path}] is Invalid")

    def write_collision_scene_config(self, config_path: str = '') -> bool:
        """Attempts to write out a collision shape configuration for reloading if needed
        NOTE: currently only handles the default config path (as set at initialisation)
        NOTE: this performs an overwrite of the collision shapes (only when called)
        """
        if config_path == '':
            config_path = self.collision_scene_default_path
            rospy.logwarn(f"[SAVE COLLISION SCENE] -> Collision Shape Config Path Empty, defaulting to {self.collision_scene_default_path}")

        # Create the path if not in existance        
        if not os.path.exists(os.path.dirname(config_path)):
            os.makedirs(os.path.dirname(config_path))

        # Create a new Dictionary to Overwrite Current Config
        config = {}
        for key, value in self.dynamic_collision_dict.items():
            # New Dict of Items to Populate
            dict_items = {}

            # Setup the shape type for saving in a simple manner
            dict_items['shape'] = value.shape.stype
            if hasattr(value.shape, 'radius'):
                dict_items['radius'] = value.shape.radius
            if hasattr(value.shape, 'length'):
                dict_items['length'] = value.shape.length
            if hasattr(value.shape, 'scale'):
                dict_items['scale_x'] = float(value.shape.scale[0])
                dict_items['scale_y'] = float(value.shape.scale[1])
                dict_items['scale_z'] = float(value.shape.scale[2])

            # Save the shape ID (should be unique)
            dict_items['id'] = value.id

            # Save the Physical Position of Object in Scene 
            # NOTE: currently only supports poses from base_link
            current_shape_se3 = sm.SE3(self.collision_dict[key][0].T)
            uq = UnitQuaternion(current_shape_se3)
            shape_pose = Pose(
                position=Point(*current_shape_se3.t), 
                orientation=Quaternion(*np.concatenate([uq.vec3, [uq.s]]))
            )
            dict_items['pos_x'] = float(shape_pose.position.x) 
            dict_items['pos_y'] = float(shape_pose.position.y)
            dict_items['pos_z'] = float(shape_pose.position.z)
            dict_items['rot_w'] = float(shape_pose.orientation.w)
            dict_items['rot_x'] = float(shape_pose.orientation.x)
            dict_items['rot_y'] = float(shape_pose.orientation.y)
            dict_items['rot_z'] = float(shape_pose.orientation.z)

            # Update output dictionary with a dictionary of items
            # NOTE: key is the actual shape's key
            config[key] = dict_items

        # Dump the current shape dictionary (configured for saving) to nominated path
        with open(config_path, 'w') as handle:
            handle.write(yaml.dump(config))

        rospy.loginfo(f"[SAVE COLLISION SCENE] -> Collision Scene Written to: [{config_path}]")
        return True

    # --------------------------------------------------------------------- #
    # --------- Standard Methods ------------------------------------------ #
    # --------------------------------------------------------------------- #
    def general_executor(self, q, pose: Pose = None, collision_ignore: bool =False, workspace_ignore: bool = False, move_time_sec: float = 5.0) -> bool:
        """A general executor that performs the following on a given joint state goal
        - Workspace check prior to move. Setting workspace_ignore to True skips this (for cases where a pose is not defined or needed)
        - Singularity checking and termination on failure
        - Collision checking and termination on failure. Setting collision_ignore to True skips this check
        - Execution of a ros_control or standard execution (real/sim) depending on what is available

        :param q: array like joint state of the arm
        :type q: np.ndarray
        :param pose: Pose requested to move to, defaults to None
        :type pose: Pose, optional
        :param collision_ignore: True to ignore collision checking, defaults to False
        :type collision_ignore: bool, optional
        :param workspace_ignore: True to ignore workspace checking, defaults to False
        :type workspace_ignore: bool, optional
        :param move_time_sec: Time to take to move to pose, defaults to 5 seconds
        :type move_time_sec: float, optional
        :return: True on success or False
        :rtype: bool
        """
        result = False
        if self.pose_within_workspace(pose) == False and not workspace_ignore:
            rospy.logwarn("[GENERAL EXECUTOR] -> Pose goal outside defined workspace; refusing to move...")
            return result

        if self.check_singularity(q):
            rospy.logwarn(f"[GENERAL EXECUTOR] -> Singularity Detected in Goal State: {q}")
            return result

        # Generate trajectory from successful solution
        rospy.loginfo(f"time to move: {move_time_sec}")
        traj = self.traj_generator(self, qf=q, move_time_sec=move_time_sec)

        if traj.name == 'invalid':
            rospy.logwarn(f"[GENERAL EXECUTOR] -> Invalid trajectory detected, existing safely")
            return result

        # Take max time from trajectory and convert to array based on traj length
        # NOTE: this is needed to then construct a JointTrajectory type
        max_time = np.max(traj.t)

        # Conduct 'Ghost' Robot Check for Collision throughout trajectory
        # NOTE: also publishes marker representation of trajectory for visual confirmation (Rviz)
        if collision_ignore:
            go_signal = True
        else:
            go_signal = self.trajectory_collision_checker(traj=traj)
        
        # If valid (no collisions detected) then continue with action
        if go_signal:
            # Check if the robot has been initialised to connect to real hardware
            # In these cases, the joint_trajectory_controller is to be used
            if self.hw_controlled:
                rospy.loginfo(f"[GENERAL EXECUTOR] -> Running ros_control trajectory method...")
                self.controller_select(ControllerType.JOINT_TRAJECTORY)
                result = self.execute_ros_control_trajectory(traj=traj, max_time=max_time)
                self.controller_select(ControllerType.JOINT_GROUP_VEL)
            else:
                rospy.loginfo(f"[GENERAL EXECUTOR] -> Running ros_control joint group velocity method...")
                # Not in ROS Backend (i.e., using real hardware) so default to
                # standard implementation - largely for simulation
                self.executor = TrajectoryExecutor(
                    self,
                    traj=traj,
                    cutoff=self.trajectory_end_cutoff
                )

                while not self.executor.is_finished():
                    rospy.sleep(0.01)

                result = self.executor.is_succeeded()

            # Send empty data at end to clear visual trajectory
            marker_traj = Marker()
            marker_traj.header.frame_id = self.base_link.name
            marker_traj.points = []
            self.display_traj_publisher.publish(marker_traj)

        return result
    
    def controller_switch_service(self, ns, cls, **kwargs):
        """Sends data to a ROS service
            Credit: https://github.com/ros-controls/ros_control/issues/511 

        Args:
            ns (str): namepsace of service
        """
        rospy.wait_for_service(ns)
        service = rospy.ServiceProxy(ns, cls)
        response = service(**kwargs)
        if not response.ok:
            rospy.logwarn(f"[CONTROLLER SWITCHER] -> Attempting controller switch failed...")
        else:
            rospy.loginfo(f"[CONTROLLER SWITCHER] -> Controller switched successfully")

    def controller_select(self, controller_type: int = 0) -> bool:
        """Attempts to select a provided controller type via controller_manager service

        Args:
            controller_type (int, optional): Enumerated value defining a controller type 
            (only 0 [joint velocity], and 1 [trajectory] are supported). Defaults to 0.

        Returns:
            bool: True if successfully switched, otherwise False
        """
        if controller_type == None or controller_type > 1 or controller_type < 0:
            rospy.logerr(f"[CONTROLLER SELECT] -> invalid controller type: {controller_type}")
            return False
        
        if self.trajectory_controller == None or self.velocity_controller == None:
            rospy.logerr(f"[CONTROLLER SELECT] -> controller names not configured correctly")
            rospy.logwarn(f"[CONTROLLER SELECT] -> configured trajectory controller: {self.trajectory_controller}")
            rospy.logwarn(f"[CONTROLLER SELECT] -> controller velocity controller: {self.velocity_controller}")
            return False
        
        if controller_type == ControllerType.JOINT_GROUP_VEL and controller_type != self.controller_type:
            try:
               self.controller_switch_service("/controller_manager/switch_controller",
                       SwitchController,
                       start_controllers=[self.velocity_controller],
                       stop_controllers=[self.trajectory_controller],
                       strictness=1, start_asap=False, timeout=0.0)
               
               self.controller_type = ControllerType.JOINT_GROUP_VEL
            except rospy.ServiceException as e:
               rospy.logerr(f"[CONTROLLER SELECT] -> Could not switch controllers: {e}")
               return False
        elif controller_type == ControllerType.JOINT_TRAJECTORY and controller_type != self.controller_type:
            try:
               self.controller_switch_service("/controller_manager/switch_controller",
                       SwitchController,
                       start_controllers=[self.trajectory_controller],
                       stop_controllers=[self.velocity_controller],
                       strictness=1, start_asap=False, timeout=0.0)
               self.controller_type = ControllerType.JOINT_TRAJECTORY
            except rospy.ServiceException as e:
               rospy.logerr(f"[CONTROLLER SELECT] -> Could not switch controllers: {e}")
               return False
        else:
            rospy.logerr(f"[CONTROLLER SELECT] -> Unknown controller type, failed.")
            return False

    def execute_ros_control_trajectory(self, traj, max_time: int = 0):
        """
        Executes a ros_control trajectory implementation
        NOTE: only works when ros_control backend is available
        """
        # TODO: needs to handle a namespace in the topic (dependent on robot type)
        controller_action = "/" + self.trajectory_controller + "/follow_joint_trajectory"
        print(f"controller action: {controller_action}")
        # Create a client to loaded controller
        client = actionlib.SimpleActionClient(
            controller_action, 
            FollowJointTrajectoryAction
        )

        # Wait for client server connection
        # NOTE: timeout on 5 seconds
        if not client.wait_for_server(timeout=rospy.Duration(secs=5)):
            rospy.logerr(f"[EXECUTE ROS CONTROL TRAJ] -> Could not setup client for ros_control trajectory controller")
            return False

        # Iterates through calculated trajectory and converts to a JointTrajectory type
        # NOTE: a standard delay of 1 second needed so controller can execute correctly
        #       otherwise, issues with physical panda motion
        jt = JointTrajectory()
        jt.header.stamp = rospy.Time.now()
        jt.joint_names = list(self.joint_names)
        time_array = np.linspace(0, max_time, len(traj.s))
        #print(f"time array: {time_array}")
        #print(f"traj joint names: {jt_traj.joint_names}")
        for idx in range(0,len(traj.s)):
            jt_traj_point = JointTrajectoryPoint()
            jt_traj_point.time_from_start = rospy.Duration(time_array[idx] + 1)
            jt_traj_point.positions = list(traj.s[idx])
            jt_traj_point.velocities = list(traj.sd[idx])
            jt.points.append(jt_traj_point)
        
        # Create goal
        goal = FollowJointTrajectoryGoal()
        goal.trajectory = jt

        # Send Joint Trajectory to position controller for execution
        client.send_goal(goal)

        # TODO: currently the collision preempt (or any armer preempt)
        #       does not stop the motion as it is being externally controlled
        #       through the joint trajectory controller. Best to add a supervisor
        #       to stop action (trajectory controller) on any events here
        # Wait for controller to finish
        client.wait_for_result()
        result = client.get_result()

        if hasattr(result, 'error_code') and result.error_code == 0:
            rospy.loginfo(f"[EXECUTE ROS CONTROL TRAJ] -> Successful execution of ros_control trajectory -> [{result}]")
            return True
        else:
            rospy.logerr(f"[EXECUTE ROS CONTROL TRAJ] -> Error found in executing ros_control trajectory -> [{result}]")
            return False

    def close(self):
        """
        Closes the action servers associated with this robot
        """
        self.pose_server.need_to_terminate = True
        self.joint_pose_server.need_to_terminate = True
        self.named_pose_server.need_to_terminate = True

    def preempt(self, *args: list) -> None:
        """
        Stops any current motion
        """
        # pylint: disable=unused-argument
        if self.executor:
            self.executor.abort()

        # Warn and Reset
        if self.singularity_approached:
            rospy.logwarn(f"PREEMPTED: Approaching singularity (index: {self.manip_scalar}) --> please signal /arm/recover service to fix")
            self.singularity_approached = False

        if self.collision_approached:
            rospy.logwarn(f"PREEMPTED: Collision Found --> please signal /arm/collision_recover service to fix")
            self.collision_approached = False

        # NOTE: put robot object into ERROR state as we have been preempted
        #       currently only the Home action resets this back to ControlMode.Joints
        # TODO: need to ensure this is checked for most control inputs (currently handled in Cartesian and Joint Velocity callbacks)
        # TODO: need to check other methods of reset if needed.
        self.preempted = True
        self._controller_mode = ControlMode.ERROR
        self.last_update = 0

    def preempt_tracking(self, *args: list) -> None:
        """
        Stops any current motion
        """
        # pylint: disable=unused-argument
        if self.executor:
            self.executor.abort()

        # Warn and Reset
        if self.singularity_approached:
            rospy.logwarn(f"PREEMPTED: Approaching singularity (index: {self.manip_scalar}) --> please home to fix")
            self.singularity_approached = False

        self.preempted = True
        self._controller_mode = ControlMode.JOINTS
        self.last_update = 0

    def preempt_other(self, *args: list) -> None:
        """
        Stops any current motion
        """
        # pylint: disable=unused-argument
        if self.executor:
            self.executor.abort()

        # Warn and Reset
        if self.singularity_approached:
            rospy.logwarn(f"PREEMPTED: Approaching singularity (index: {self.manip_scalar}) --> please home to fix")
            self.singularity_approached = False

        self.preempted = True
        self._controller_mode = ControlMode.JOINTS
        self.last_update = 0

    def __vel_move(self, twist_stamped: TwistStamped) -> None:
        target: Twist = twist_stamped.twist

        if twist_stamped.header.frame_id == '':
            twist_stamped.header.frame_id = self.base_link.name

        e_v = np.array([
            target.linear.x,
            target.linear.y,
            target.linear.z,
            target.angular.x,
            target.angular.y,
            target.angular.z
        ])
        
        if np.any(e_v - self.e_v) or self._controller_mode == ControlMode.JOINTS:
            self.e_p = self.fkine(self.q, start=self.base_link, end=self.gripper)

        self.e_v = e_v
        self.e_v_frame = twist_stamped.header.frame_id
        
        self._controller_mode = ControlMode.CARTESIAN
        self.last_update = rospy.get_time()

    def pose_within_workspace(self, pose=Pose):
        if pose == None or pose == Pose():
            rospy.logerr(f"[WORKSPACE CHECK] -> Invalid Pose Input [{pose}]")
            return False
        
        # TODO: The workspace should be stored in param server!!!
        path = rospy.get_param('/robot_workspace', '')
        if path == '':
            # No workspace defined - assume infinite workspace
            # TODO: remove debug message
            rospy.logwarn("[WORKSPACE CHECK] -> No WORKSPACE DEFINED")
            return True
        
        if os.path.isfile(path) == False:
            # In this case fail safe as a workspace definition had been attempted by user
            rospy.logerr(f"[WORKSPACE CHECK] -> [{self.name}] WORKSPACE COULD NOT BE DEFINED! (Invalid Path) Please check the workspace.yaml file located at {path}")
            return False
        
        # TODO: The workspace should be stored in param server
        # TODO: The workspace should only update when requested rather than re-sourcing constantly
        with open(path, 'r') as handle:
            config = yaml.load(handle, Loader=yaml.SafeLoader)

        workspace = config['workspace'] if 'workspace' in config else None
        rospy.logdebug(f"[WORKSPACE CHECK] -> Boundary--: {workspace}")

        if workspace == None:
            rospy.logerr(f"[WORKSPACE CHECK] -> [{self.name}] WORKSPACE COULD NOT BE DEFINED! (Invalid Yaml) Please check the workspace.yaml file located at {path}")
            # In this case fail safe as a workspace definition had been attempted by user
            return False

        min_x = workspace[0]['min'][0]['x']
        min_y = workspace[0]['min'][0]['y']
        min_z = workspace[0]['min'][0]['z']

        max_x = workspace[1]['max'][0]['x']
        max_y = workspace[1]['max'][0]['y']
        max_z = workspace[1]['max'][0]['z']

        # Check that cartesian position of end-effector is within defined constraints. 
        # NOTE: the following bounds are based from the base_link which is the origin point. 
        # Assumed main constraint is z-axis plane (added a y-axis/End-Point) plane termination condition as well
        # NOTE: this is assuming Left-to-Right motion w.r.t Robot base_link
        if(pose.position.x <= min_x or pose.position.x >= max_x or \
            pose.position.y <= min_y or pose.position.y >= max_y or \
                pose.position.z <= min_z or pose.position.z >= max_z):

            rospy.logerr(f"[WORKSPACE CHECK] -> [{self.name}] ROBOT **would** EXCEEDED DEFINED BOUNDARY!!!")
            rospy.logerr(f"[WORKSPACE CHECK] -> [{self.name}] - Goal pose.position: {pose.position}")
            return False
        else:
            return True
        
    def get_state(self) -> ManipulatorState:
        """
        Generates a ManipulatorState message for the robot

        :return: ManipulatorState message describing the current state of the robot
        :rtype: ManipulatorState
        """
        jacob0 = self.jacob0(self.q, end=self.gripper)
        
        ## end-effector position
        ee_pose = self.ets(start=self.base_link, end=self.gripper).eval(self.q)
        header = Header()
        header.frame_id = self.base_link.name
        header.stamp = rospy.Time.now()

        pose_stamped = PoseStamped()
        pose_stamped.header = header

        translation = ee_pose[:3, 3]    
        pose_stamped.pose.position.x = translation[0]
        pose_stamped.pose.position.y = translation[1]
        pose_stamped.pose.position.z = translation[2]

        rotation = ee_pose[:3, :3]
        ee_rot = sm.UnitQuaternion(rotation)

        pose_stamped.pose.orientation.w = ee_rot.A[0]
        pose_stamped.pose.orientation.x = ee_rot.A[1]
        pose_stamped.pose.orientation.y = ee_rot.A[2]
        pose_stamped.pose.orientation.z = ee_rot.A[3]

        state = ManipulatorState()
        state.ee_pose = pose_stamped

        try:
            # end-effector velocity
            T = jacob0 @ self.qd
        except:
            state.ee_velocity = TwistStamped()
            return state

        twist_stamped = TwistStamped()
        twist_stamped.header = header
        twist_stamped.twist.linear.x = T[0]
        twist_stamped.twist.linear.y = T[1]
        twist_stamped.twist.linear.z = T[2]
        twist_stamped.twist.angular.x = T[3]
        twist_stamped.twist.angular.x = T[4]
        twist_stamped.twist.angular.x = T[5]

        state.ee_velocity = twist_stamped
        
        # joints
        if self.joint_states:
            state.joint_poses = np.array(self.joint_states.position)[self.joint_indexes]
            state.joint_velocities = np.array(self.joint_states.velocity)[self.joint_indexes]
            state.joint_torques = np.array(self.joint_states.effort)[self.joint_indexes]
        
        else:
            state.joint_poses = list(self.q)
            state.joint_velocities = list(self.qd)
            state.joint_torques = np.zeros(self.n)
        
        return state

    def test_guards(
        self,
        guards: Guards,
        start_time: float) -> int:

        triggered = 0

        if (guards.enabled & guards.GUARD_DURATION) == guards.GUARD_DURATION:
            triggered |= guards.GUARD_DURATION if rospy.get_time() - start_time > guards.duration else 0

        if (guards.enabled & guards.GUARD_EFFORT) == guards.GUARD_EFFORT:
            eActual = np.fabs(np.array([
                self.state.ee_wrench.wrench.force.x,
                self.state.ee_wrench.wrench.force.y,
                self.state.ee_wrench.wrench.force.z,
                self.state.ee_wrench.wrench.torque.x,
                self.state.ee_wrench.wrench.torque.y,
                self.state.ee_wrench.wrench.torque.z,
            ]))

            eThreshold = np.array([
                guards.effort.force.x,
                guards.effort.force.y,
                guards.effort.force.z,
                guards.effort.torque.x,
                guards.effort.torque.y,
                guards.effort.torque.z,
            ])

            triggered |= guards.GUARD_EFFORT if np.any(eActual > eThreshold) else 0
            
        return triggered

    def publish(self):
        self.joint_publisher.publish(Float64MultiArray(data=self.qd))

    def workspace_display(self):
        """
        TODO: a method to display (debugging) the currently configured workspace of the arm
        NOTE: should only run this once every time a new workspace/change to workspace has been done
        """
        pass
    
    def int_marker_cb(self, feedback):
        """
        On mouse release (valid click event) this method updates the corresponding
        object pose in the collision dictionary
        """
        # Update pose of shape on a mouse release event
        if feedback.event_type == InteractiveMarkerFeedback.MOUSE_UP:            
            # Update collision dict shape information
            shape = self.collision_dict[feedback.marker_name] if feedback.marker_name in self.collision_dict else None
            if shape == None or len(shape) > 1 or len(shape) <= 0:
                rospy.logerr(f"[INTERACTIVE MARKER UPDATE] -> Could not access [{feedback.marker_name}]")
            else:
                pose_se3 = SE3(feedback.pose.position.x, 
                        feedback.pose.position.y,
                        feedback.pose.position.z) * UnitQuaternion(feedback.pose.orientation.w, [
                                                                feedback.pose.orientation.x, 
                                                                feedback.pose.orientation.y,
                                                                feedback.pose.orientation.z]).SE3()
                shape[0].T = pose_se3.A
            

            # Characterise collision overlaps when change is found
            # NOTE: enable to see what links are overlapped for debugging purposes
            self.characterise_collision_overlaps(debug=False)

            # Debugging
            # rospy.loginfo(f"[INTERACTIVE MARKER UPDATE] -> Obj {feedback.marker_name} updated to pose: {feedback.pose}")
            self.interactive_marker_server.applyChanges()
    
    def normalizeQuaternion( self, quaternion_msg ):
        """
        This is from: https://github.com/ros-visualization/visualization_tutorials/tree/noetic-devel
        """
        norm = quaternion_msg.x**2 + quaternion_msg.y**2 + quaternion_msg.z**2 + quaternion_msg.w**2
        s = norm**(-0.5)
        quaternion_msg.x *= s
        quaternion_msg.y *= s
        quaternion_msg.z *= s
        quaternion_msg.w *= s

    def interactive_marker_creation(self):
        """NOTE: updated and implemented from the tutorials here: https://github.com/ros-visualization/visualization_tutorials/tree/noetic-devel
        Publishes interactive marker versions of added shape objects
        Currently handles only: 
        - Sphere 
        - Cylinder
        - Cuboid
        """
        dyn_col_dict_cp = self.dynamic_collision_dict.copy()
        for obj in list(dyn_col_dict_cp.values()):
            # Ensure object has been successfully added to backend first
            # print(f"here: {obj}")
            if not obj.marker_created:
                # Default interactive marker setup
                marker = Marker()
                if obj.shape.stype == 'sphere':
                    marker.type = Marker.SPHERE

                    # Expects diameter (m)
                    marker.scale.x = obj.shape.radius * 2
                    marker.scale.y = obj.shape.radius * 2
                    marker.scale.z = obj.shape.radius * 2
                elif obj.shape.stype == 'cylinder':
                    marker.type = Marker.CYLINDER

                    # Expects diameter (m)
                    marker.scale.x = obj.shape.radius * 2
                    marker.scale.y = obj.shape.radius * 2
                    marker.scale.z = obj.shape.length
                elif obj.shape.stype == 'cuboid':
                    marker.type = Marker.CUBE

                    # Expects diameter (m)
                    marker.scale.x = obj.shape.scale[0]
                    marker.scale.y = obj.shape.scale[1]
                    marker.scale.z = obj.shape.scale[2]
                else:
                    break

                int_marker = InteractiveMarker()
                int_marker.header.frame_id = self.base_link.name
                int_marker.scale = np.max([marker.scale.x, marker.scale.y, marker.scale.z])
                int_marker.pose = obj.pose
                int_marker.name = obj.key
                marker.color.r = obj.shape.color[0]
                marker.color.g = obj.shape.color[1]
                marker.color.b = obj.shape.color[2]
                marker.color.a = obj.shape.color[3]
                
                # Main Motion
                control = InteractiveMarkerControl()
                control.always_visible = True
                control.interaction_mode = InteractiveMarkerControl.MOVE_ROTATE_3D
                control.markers.append(marker)
                int_marker.controls.append(control)  

                # Axis Movement in X
                control = InteractiveMarkerControl()
                control.name = "move_x"
                control.orientation.w = 1
                control.orientation.x = 1
                control.orientation.y = 0
                control.orientation.z = 0
                self.normalizeQuaternion(control.orientation)
                control.interaction_mode = InteractiveMarkerControl.MOVE_AXIS
                control.orientation_mode = InteractiveMarkerControl.FIXED
                int_marker.controls.append(control)        

                # Axis Movement in Y
                control = InteractiveMarkerControl()
                control.name = "move_y"
                control.orientation.w = 1
                control.orientation.x = 0
                control.orientation.y = 1
                control.orientation.z = 0
                self.normalizeQuaternion(control.orientation)
                control.interaction_mode = InteractiveMarkerControl.MOVE_AXIS
                control.orientation_mode = InteractiveMarkerControl.FIXED
                int_marker.controls.append(control)  

                # Axis Movement in Z
                control = InteractiveMarkerControl()
                control.name = "move_z"
                control.orientation.w = 1
                control.orientation.x = 0
                control.orientation.y = 0
                control.orientation.z = 1
                self.normalizeQuaternion(control.orientation)
                control.interaction_mode = InteractiveMarkerControl.MOVE_AXIS
                control.orientation_mode = InteractiveMarkerControl.FIXED
                int_marker.controls.append(control)  

                # Axis Rotation in X
                control = InteractiveMarkerControl()
                control.name = "rotate_x"
                control.orientation.w = 1
                control.orientation.x = 1
                control.orientation.y = 0
                control.orientation.z = 0
                self.normalizeQuaternion(control.orientation)
                control.interaction_mode = InteractiveMarkerControl.ROTATE_AXIS
                control.orientation_mode = InteractiveMarkerControl.FIXED
                int_marker.controls.append(control)  

                # Axis Rotation in Y
                control = InteractiveMarkerControl()
                control.name = "rotate_y"
                control.orientation.w = 1
                control.orientation.x = 0
                control.orientation.y = 1
                control.orientation.z = 0
                self.normalizeQuaternion(control.orientation)
                control.interaction_mode = InteractiveMarkerControl.ROTATE_AXIS
                control.orientation_mode = InteractiveMarkerControl.FIXED
                int_marker.controls.append(control)  

                # Axis Rotation in Z
                control = InteractiveMarkerControl()
                control.name = "rotate_z"
                control.orientation.w = 1
                control.orientation.x = 0
                control.orientation.y = 0
                control.orientation.z = 1
                self.normalizeQuaternion(control.orientation)
                control.interaction_mode = InteractiveMarkerControl.ROTATE_AXIS
                control.orientation_mode = InteractiveMarkerControl.FIXED
                int_marker.controls.append(control)  
                
                obj.marker_created = True
                self.interactive_marker_server.insert(marker=int_marker, feedback_cb=self.int_marker_cb)
                self.interactive_marker_server.applyChanges()

    def step(self, dt: float = 0.01) -> None:  # pylint: disable=unused-argument
        """
        Updates the robot joints (robot.q) used in computing kinematics
        :param dt: the delta time since the last update, defaults to 0.01
        :type dt: float, optional
        """
        if self.readonly:
            return

        current_time = rospy.get_time()
        self.state = self.get_state()

        # PREEMPT motion on any detected state errors or singularity approach
        if self.state.errors != 0 \
            or self.check_singularity(self.q):
            # print(f"preempting...")
            self.preempt()

        # calculate joint velocities from desired cartesian velocity
        if self._controller_mode == ControlMode.CARTESIAN:
            if current_time - self.last_update > 0.1:
                self.e_v *= 0.9 if np.sum(np.absolute(self.e_v)
                                          ) >= 0.0001 else 0

                if np.all(self.e_v == 0):
                    self._controller_mode = ControlMode.JOINTS

            try:
                _, orientation = self.tf_listener.lookupTransform(
                    self.base_link.name,
                    self.e_v_frame,
                    rospy.Time(0)
                )
                
                U = UnitQuaternion([
                    orientation[-1],
                    *orientation[:3]
                ], norm=True, check=False).SE3()
                
                e_v = np.concatenate((
                (U.A @ np.concatenate((self.e_v[:3], [1]), axis=0))[:3],
                (U.A @ np.concatenate((self.e_v[3:], [1]), axis=0))[:3]
                ), axis=0)
                
                # Calculate error in base frame
                p = self.e_p.A[:3, 3] + e_v[:3] * dt                     # expected position
                Rq = UnitQuaternion.RPY(e_v[3:] * dt) * UnitQuaternion(self.e_p.R)
                
                T = SE3.Rt(SO3(Rq.R), p, check=False)   # expected pose
                Tactual = self.fkine(self.q, start=self.base_link, end=self.gripper) # actual pose
                
                e_rot = (SO3(T.R @ np.linalg.pinv(Tactual.R), check=False).rpy() + np.pi) % (2*np.pi) - np.pi
                error = np.concatenate((p - Tactual.t, e_rot), axis=0)
                
                e_v = e_v + error
                
                self.e_p = T
                            
                self.j_v = np.linalg.pinv(
                self.jacob0(self.q, end=self.gripper)) @ e_v
              
            except (tf.LookupException, tf2_ros.ExtrapolationException):
              rospy.logwarn('No valid transform found between %s and %s', self.base_link.name, self.e_v_frame)
              self.preempt()

        # Conduct a Trajectory Motion 
        # NOTE: the executor step is run internally for armer (invariant of ros_control mechanism)
        # NOTE: if actually on a real robot, the hw_controlled flag (in the ROS backend) handles the joint trajectory controller execution
        if self.executor and not self.hw_controlled:
            self.j_v = self.executor.step(dt)

            # TODO: Remove?
            # - Need to validate if this is still required
            # - - IF it is required the rounding limit should be a param
            # if any(False if np.absolute(v) >= 0.000000001 else True for v in self.j_v):
            #     rospy.logerr(f"We are rounding a joint velocity in the executor...{self.j_v}")
            
            # bob = [v if np.absolute(v) >= 0.000000001 else 0 for v in self.j_v]
            # if np.any(self.j_v != bob):
            #     rospy.logerr(f"-- overwriting join velocities with rounded version...{bob}")
            #     self.j_v = bob
        else:
            # TODO: Remove?
            # - Need to validate if this is still required
            # - - IF it is required the rounding limit should be a param
            # if any(False if np.absolute(v) >= 0.0001 else True for v in self.j_v):
            #     rospy.logerr(f"We are rounding a joint velocity without executor...{self.j_v}")

            # bob = [v if np.absolute(v) >= 0.0001 else 0 for v in self.j_v]
            # if np.any(self.j_v != bob):
            #     rospy.logerr(f"-- overwriting join velocities with rounded version...{bob}")
            #     self.j_v = bob

            # Needed for preempting joint velocity control
            # - this is painfully hard coded...
            # -- should it be the sum or just an any(abs(value) >= min)?
            # -- should the else case be an array of zeros?
            if any(self.j_v) and current_time - self.last_update > 0.1:
                self.j_v = [v * 0.9 if np.absolute(v) > 0 else 0 for v in self.j_v]
            
        self.qd = self.j_v
        self.last_tick = current_time

        self.state_publisher.publish(self.state)

        self.event.set()

    def __load_config(self):
        """[summary]
        """
        self.named_poses = {}
        for config_path in self.custom_configs:
            try:
                config = yaml.load(open(config_path), Loader=yaml.SafeLoader)
                if config and 'named_poses' in config:
                    self.named_poses.update(config['named_poses'])
            except IOError:
                rospy.logwarn(
                    'Unable to locate configuration file: {}'.format(config_path))

        if os.path.exists(self.config_path):
            try:
                config = yaml.load(open(self.config_path),
                                   Loader=yaml.SafeLoader)
                if config and 'named_poses' in config:
                    self.named_poses.update(config['named_poses'])
            except IOError:
                pass

    def __write_config(self, key: str, value: Any, config_path: str=''):
        """[summary]

        :param key: [description]
        :type key: str
        :param value: [description]
        :type value: Any
        """
        if config_path == '':
            # Use default config_path
            # NOTE: the default config_path is /home/.ros/configs/system_named_poses.yaml
            config_path = self.config_path

        if not os.path.exists(os.path.dirname(config_path)):
            os.makedirs(os.path.dirname(config_path))

        config = {}

        try:
            with open(config_path) as handle:
                current = yaml.load(handle.read(), Loader=yaml.SafeLoader)

                if current:
                    config = current

        except IOError:
            pass

        config.update({key: value})

        with open(config_path, 'w') as handle:
            handle.write(yaml.dump(config))
