#!/usr/bin/env python3

""" Code for fuzzy logic controller and follow behavior """

import fuzzylite as fl
import rospy
from geometry_msgs.msg import Twist, Vector3
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Float32
import numpy as np
from avoidance_engine import avoidance_engine
from formation_engine import formation_engine
from fusion_engine import fusion_engine
import sys

class Follower:
    """ ROS node for follower robot controller """
    def __init__(self, robot_ns):
        rospy.init_node(f'{robot_ns}_follower')

        # Setup publishers and subscribers
        self.vel_pub = rospy.Publisher(f'/{robot_ns}/cmd_vel', Twist, queue_size=10)
        rospy.Subscriber(f'/{robot_ns}/scan', LaserScan, self.process_scan)
        rospy.Subscriber(f'/{robot_ns}/angle_to_leader', Float32, self.process_leader_angle)

        # Define instance variables
        self.all_lidar_data = None
        self.laser_distances = None
        self.offset_angle = None
        self.all_detected = None
        self.desired_distance = .75 # Desired distance to keep between leader and follower

        # Setup Fuzzy Logic Controller Inputs
        self.angle = formation_engine.input_variable('Angle')
        self.distance = formation_engine.input_variable('Distance')
        self.left_laser = avoidance_engine.input_variable('Left_Laser')
        self.right_laser = avoidance_engine.input_variable('Right_Laser')
        self.front_laser = avoidance_engine.input_variable('Front_Laser')
        self.position_measure = fusion_engine.input_variable('Position_Measure')
        self.min_laser = fusion_engine.input_variable('Min_Laser')

        # Setup Fuzzy Logic Controller Outputs
        self.vel_formation = formation_engine.output_variable('Velocity')
        self.rot_formation = formation_engine.output_variable('Rotation')
        self.vel_avoidance = avoidance_engine.output_variable('Velocity')
        self.rot_avoidance = avoidance_engine.output_variable('Rotation')
        self.formation_weight = fusion_engine.output_variable('Formation_Weight')
        self.collision_weight = fusion_engine.output_variable('Collision_Weight')

    def process_scan(self, msg):
        """ Process lidar scan data and extracts distance measurements from left, right and front """
        self.all_lidar_data = msg.ranges[:360]

        # left, right, and front lidar angles
        angles = [90, 270, 0] 
        self.laser_distances = [self.get_average_distance(theta, 7) for theta in angles]

        # For debugging purposes, collects all angles that the lidar dectects distances
        self.all_detected = [i for i, angle in enumerate(msg.ranges) if not (angle == float('inf'))]
        
    def process_leader_angle(self, msg):
        """ Process offset from leader angle determined from the AR tags """
        self.offset_angle = msg.data

    def get_average_distance(self, angle, n, distance=None):
        """ 
        Gets the average of the neighboring lidar distances for a more robust distance measurement 
        
        Instead of relying on a single lidar distance measurement, the average of the +/- n nearby 
        angles are calculated for an average distance. If a lidar distance is inf it is not included
        in the average calculation. 

        Args:
            angle: the original lidar angle
            n: number of +/- surrounding lidar angles to perform average calculation
            distance: [optional] array of lidar distances
        Returns:
            average: the average of the angle and +/- neighboring angles
        """
        if self.all_lidar_data is None and distance is None:
            return None
            
        avg, count = 0, 0
        for i in range(-n, n+1):
            neighbor = self.all_lidar_data[(angle + i) % 360]
            if neighbor != float('inf'):
                avg += neighbor
                count += 1
        
        return avg / count if count else float('inf')

    def fuzzy_formation(self):
        """ Fuzzy logic controller that determines the robot commands to keep in formation """
        if self.offset_angle is None or self.all_lidar_data is None:
            return None, None

        # Feed offset angle and offset distance as input to the fuzzy controller
        self.angle.value = self.offset_angle
        actual_offset_distance = self.get_average_distance((360-int(self.offset_angle)), 3)

        if actual_offset_distance is None:
            return None, None

        self.distance.value = self.desired_distance - actual_offset_distance

        # Perform fuzzy inference
        formation_engine.process()

        return self.vel_formation.value, self.rot_formation.value

    def fuzzy_collision_avoidance(self):
        """ Fuzzy logic controller that determines the robot commands to avoid obstacles and internal collision """
        # Skip if no laser distance data
        if self.laser_distances is None:
            return None, None

        # Feed lidar scan distances as input to the fuzzy controller
        self.left_laser.value = self.laser_distances[0] if self.laser_distances[0] else float('inf')
        self.right_laser.value = self.laser_distances[1] if self.laser_distances[0] else float('inf')
        self.front_laser.value = self.laser_distances[2] if self.laser_distances[0] else float('inf')

        # Perform fuzzy inference
        avoidance_engine.process()

        return self.vel_avoidance.value, self.rot_avoidance.value

    def fuzzy_fusion(self, v1, r1, v2, r2):
        """
        Fuzzy logic controller that combines formation and collision avoidance
        
        Args:
            v1: velocity output from fuzzy_formation()
            r1: angular_velocity output from fuzzy_formation()
            v2: velocity output from fuzzy_collision_avoidance()
            r2: angular_velocity output from fuzzy_collision_avoidance()
        Returns:
            v_final: final velocity calculated from a weighted sum of formation and collision avoidance
            r_final: final angular velocity calculated from a weighted sum of formation and collision avoidance
        """
        self.position_measure.value = abs(self.distance.value)
        self.min_laser.value = min(self.left_laser.value, self.right_laser.value, self.front_laser.value)

        # Perform fuzzy inference
        fusion_engine.process()

        # Get weights
        f_W = self.formation_weight.value
        c_W = self.collision_weight.value

        # Perform weighted sum calculation
        v_final = v1 * f_W + v2 * c_W
        r_final = r1 * f_W + r2 * c_W

        return v_final, r_final

    def run(self):
        r = rospy.Rate(5)
        while not rospy.is_shutdown():
            # Call fuzzy controllers
            v1, r1 = self.fuzzy_formation()
            v2, r2 = self.fuzzy_collision_avoidance()

            m = Twist()

            # Merge fuzzy controller outputs
            if v1 is not None and v2 is not None:
                v_final, r_final = self.fuzzy_fusion(v1, r1, v2, r2)
                
                print('Formation vel:', round(v1, 4), 'Formation rot:', round(r1, 4), \
                      'Collision vel:', round(v2, 4), 'Collision rot', round(r2, 4), \
                      'Fusion vel:', round(v_final, 4), 'Fusion rot:', round(r_final, 4))
                      
                # m.linear.x = 0
                m.linear.x = v_final
                m.angular.z =  r_final

                # Reset distance and angle variables so that old data doesn't persist
                self.all_lidar_data = None
                self.laser_distances = None
                self.offset_angle = None
            else:
                # Stop movement if there is no valid output from fuzzy controllers
                m.linear.x = 0
                m.angular.z =  0

            self.vel_pub.publish(m)

            r.sleep()

        print("Shutting down")

if __name__ == '__main__':
    # Command line argument for which robot namespace topic to subscribe to
    node = Follower(sys.argv[1])
    node.run()