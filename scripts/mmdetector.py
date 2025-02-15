#!/usr/bin/env python3
"""
 @Author: Hichem Dhouib
 @Date: 2021 
 @Last Modified by:   Hichem Dhouib 
 @Last Modified time:  
"""

import sys
import numpy as np
from mmdet.apis import inference_detector, init_detector
import rospy
import message_filters
from sensor_msgs.msg import Image , CompressedImage 
from vision_msgs.msg import Detection2D, ObjectHypothesisWithPose
from visualization_msgs.msg import Marker, MarkerArray
from logging import debug 
import cv2
import os 
from cv_bridge import CvBridge
from pathlib import Path 

from mmcv.ops import get_compiling_cuda_version, get_compiler_version

# Setup check for docker 
import mmdet
import mmcv
import torch, torchvision 
print("opencv version: ", cv2.__version__) 
print("numpy version: ", np.__version__)
print("torch version: ",torch.__version__, "| torch cuda available: ",torch.cuda.is_available())
print("mmdetection version: ",mmdet.__version__)
print("mmcv version: ", mmcv.__version__)
print("compiling cuda version: ", get_compiling_cuda_version())
print("compiler version: ", get_compiler_version())
print("python3: ",sys.version)

DELETEALL_MARKER_ID = 20
CONFIG_PATH = '/workspace/src/project_catkin_workspace/src/mmdetection_ros/scripts/yolov3_d53_320_273e_coco.py'
MODEL_PATH = '/workspace/src/project_catkin_workspace/src/mmdetection_ros/scripts/latest.pth'

def convert_depth_pixel_to_metric_coordinate(depth, pixel_x, pixel_y, camera_intrinsics):
    # float x = (pixel[0] - intrin->ppx) / intrin->fx;
    # float y = (pixel[1] - intrin->ppy) / intrin->fy;
    X = (pixel_x - camera_intrinsics[2])/camera_intrinsics[0] *depth
    Y = (pixel_y - camera_intrinsics[5])/camera_intrinsics[4] *depth
    return X, Y, depth

def deleteMarkers():
    marker = Marker()
    marker.header.frame_id = "/base_link"
    marker.action = Marker.DELETEALL
    marker.id = DELETEALL_MARKER_ID
    return marker

class Detector:
    def __init__(self, model):
        
        self.bridge = CvBridge()
        self.pub_topic_color = "/mmdet/pose_estimation/det2d/compressed"
        self.pub_topic_marker_array = "/mmdet/visualization_marker_array" 
        
        self.image_pub = rospy.Publisher(self.pub_topic_color, CompressedImage, queue_size=3)
        self.marker_array_pub = rospy.Publisher(self.pub_topic_marker_array, MarkerArray, queue_size=3)
        self.marker_array_msg = MarkerArray()
        self.sub_topic_color = "/zed2/zed_node/rgb/image_rect_color" # "/spencer/sensors/rgbd_front_top/color/image_raw"
        self.sub_topic_depth = "/zed2/zed_node/depth/depth_registered" # "/spencer/sensors/rgbd_front_top/depth/image_rect_raw" # this could be wrong. subscribe to aligned depth 
        self.sub_topic_cameraInfo =  "/zed2/zed_node/depth/camera_info" # "/camera/color/camera_info"
        self.image_sub = message_filters.Subscriber(self.sub_topic_color,Image)
        self.depth_sub = message_filters.Subscriber(self.sub_topic_depth,Image)

        self.model = model
        self.score_thr= 0.6
        self.marker_count = 0 
        self.object_count = -1
        self.marker_location = None
        # Roslaunch params 
        self.visualization_3d = rospy.get_param("visualization_3d")
        self.visualization_2d = rospy.get_param("visualization_2d")
        
        self.camera_intrinsics = [266.2339172363281, 0.0, 335.1106872558594, 0.0, 266.2339172363281, 176.05209350585938, 0.0, 0.0, 1.0]
        #self.camera_intrinsics =  [526.5637817382812, 0.0, 639.7659301757812, 0.0, 526.5637817382812, 342.4459228515625, 0.0, 0.0, 1.0] # depthregistered/camera info

        self.robot_dict = { 0: "tiago", 1: "pepper"  , 2: "kuka" }
        self.bbox3D_kuka_colors = [0 , 0 , 1 , 0.6]  
        self.bbox3D_pepper_colors = [0 , 1 , 0, 0.6]
        self.bbox3D_tiago_colors = [1, 0 , 0 , 0.6]
        self.colors_dict = {0 : self.bbox3D_tiago_colors , 1 : self.bbox3D_pepper_colors , 2 : self.bbox3D_kuka_colors }


    def callback(self, image, depth_data):
        rospy.logdebug(".#.#. Callback .#.#.")

        deleteMarker = deleteMarkers()
        self.marker_array_msg.markers.append(deleteMarker) 

        depth_value = 0
        self.marker_count = 0 

        image_np = np.frombuffer(image.data, dtype = np.uint8).reshape(image.height, image.width, -1)
        # convert bgra to rgba and pass the image without the alpha parameter
        image_rgba = cv2.cvtColor(image_np, cv2.COLOR_BGRA2RGBA)
        detectionResults = inference_detector(self.model,  image_rgba[ : , : , :3])             

        dImage =  np.frombuffer(depth_data.data,  dtype = np.float32).reshape(depth_data.height, depth_data.width, -1)

        self.object_count = -1      
        for detectedRobot in range(len(detectionResults)):
            self.object_count += 1
            for subResult in detectionResults[detectedRobot]:

                if subResult.shape != (0, 5):
                    det2dobj = Detection2D()
                    det2dobj.header = image.header
                    det2dobj.source_img = image
                    det2dobj.bbox.center.x = (subResult[0] + subResult[2]) / 2
                    det2dobj.bbox.center.y = (subResult[1] + subResult[3]) / 2
                    det2dobj.bbox.size_x = subResult[2] - subResult[0]
                    det2dobj.bbox.size_y = subResult[3] - subResult[1]
                    objHypothesis = ObjectHypothesisWithPose()
                    objHypothesis.score = subResult[4]
                    det2dobj.results.append(objHypothesis)
                    score = det2dobj.results[0].score
                    rospy.logdebug("score for %s  | nr: %s | score: %s", self.robot_dict[detectedRobot] ,self.object_count, score)    
                    ######################################################################
                    ############################ Visualisation ###########################
                    ######################################################################

                    if score > self.score_thr :                    
                        ######################################################################
                        ############################### 2D BBOX ##############################
                        ######################################################################
                        if self.visualization_2d is True:
                            start_point = (int(det2dobj.bbox.center.x - det2dobj.bbox.size_x/2) ,int(det2dobj.bbox.center.y-det2dobj.bbox.size_y/2))
                            end_point = (int(det2dobj.bbox.center.x + det2dobj.bbox.size_x/2) , int(det2dobj.bbox.center.y+det2dobj.bbox.size_y/2))
                            if detectedRobot == 0: 
                                # colors are in bgr format 
                                cv_img = cv2.rectangle(image_np, start_point, end_point, (0, 0, 255), 3) # tiago 
                            elif detectedRobot == 1: 
                                cv_img = cv2.rectangle(image_np, start_point, end_point, (0, 255, 0), 3) # pepper 
                            elif detectedRobot == 2: 
                                cv_img = cv2.rectangle(image_np, start_point, end_point, (255, 0, 0), 3) # kuka

                            rospy.logdebug("2D bbox for %s | nr: %s | score: %s", self.robot_dict[detectedRobot] ,self.object_count, score)
                            cv_img = self.bridge.cv2_to_compressed_imgmsg(cv_img)
                            self.image_pub.publish(cv_img)

                        ######################################################################
                        ############################### 3D BBOX ##############################
                        ######################################################################
                        if self.visualization_3d is True:
                            self.marker_count += 1

                            converted_y = int(det2dobj.bbox.center.y)
                            converted_x = int(det2dobj.bbox.center.x)

                            depth_value = dImage[converted_y, converted_x]
                            rospy.logdebug("3D bbox for %s | nr: %s score: %s | depth: %s ", self.robot_dict[detectedRobot] ,self.object_count, score, depth_value)    

                            
                            self.marker_location = convert_depth_pixel_to_metric_coordinate(depth_value, det2dobj.bbox.center.x, det2dobj.bbox.center.y, self.camera_intrinsics)
                            scale = 1
                            
                            marker = Marker()
                            marker.header.frame_id =  "base_link" #"rgbd_front_top_link"  
                            marker.header.stamp    = rospy.get_rostime()
                            marker.type = marker.CUBE 
                            marker.id = self.marker_count 
                            
                            marker.action = Marker.ADD
                            marker.pose.position.x = self.marker_location[2] # axe: in-out of the screen 
                            marker.pose.position.y = -self.marker_location[0] # right-left 
                            marker.pose.position.z = -self.marker_location[1] # up-down
                            marker.color.r , marker.color.g , marker.color.b , marker.color.a =  self.colors_dict[detectedRobot]
    
                            marker.scale.x = scale /2
                            marker.scale.y = scale /2 
                            marker.scale.z = scale * 2         

                            rospy.logdebug(" -- Appending new marker to markerarray --")                        
                            self.marker_array_msg.markers.append(marker) 
                        else: 
                            pass

        rospy.logdebug("pub mka")
        self.marker_array_pub.publish(self.marker_array_msg.markers)
        

def main(args):

    rospy.init_node('mmdetector', log_level=rospy.DEBUG) # INFO 

    # FIXME: add to class and refactor with # os.path.join(os.path.dirname(sys.path[0]),'scripts', CONFIG_NAME)    
    # rospy.loginfo("File Path: %s ", Path(__file__).parent) 
    # rospy.loginfo("File Path absolute: %s ", Path(__file__).parent.absolute()) 
    # rospy.loginfo("Directory Path: %s", Path().absolute())
    #CONFIG_PATH = 'yolov3_d53_320_273e_coco.py'
    #MODEL_PATH = 'latest.pth'
    # parentDirectory = Path(__file__).parent.absolute()
    # rospy.loginfo("ParentDirectorzy: %s", parentDirectory)
    # configFile = Path(parentDirectory , CONFIG_PATH + '.py') # .with_suffix('.py')  
    # modelPath =  Path(parentDirectory , MODEL_PATH + '.pth') # .with_suffix('.pth')
    # model = init_detector(configFile, modelPath, device='cuda:0')
    #rospy.loginfo("configFile: %s \nmodelPath: %s", configFile, modelPath)
    
    model = init_detector(CONFIG_PATH, MODEL_PATH, device='cuda:0')
    detector = Detector(model)
    ts = message_filters.ApproximateTimeSynchronizer([detector.image_sub, detector.depth_sub], queue_size=1, slop=0.5, allow_headerless=True)
    ts.registerCallback(detector.callback)

    rospy.spin()
    
if __name__=='__main__':
    main(sys.argv)

# FIXME: add neural network path and config file to class and refactor with # os.path.join(os.path.dirname(sys.path[0]),'scripts', CONFIG_NAME)   
# TODO: replace object_count with enumarate 
# TODO: delete local variables
# TODO: (maybe not) subscribe to published human markers and add them to marker array for a synchronized data publication for all tracked objects (human + robots)
        # currently human tracking is being run independently from this file
# TODO: consider publishing the markers array, use a while not rospy.is_shutdown: 
# TODO: extract camera infos from depth info at the beginning of callback? depends if camera intrinsics change when moving the camera ????? research needed 
    #   _intrinsics.fx = cameraInfo.K[0]
    #   _intrinsics.ppx = cameraInfo.K[2]
    #   _intrinsics.ppy = cameraInfo.K[5]
    #   _intrinsics.fy = cameraInfo.K[4]
# TODO: could be optimized by storing the results in memory # def convert_depth_pixel_to_metric_coordinate see functools and optimization in trello notes 
# TODO: define class for markers? create __init__ to initialize attributes and use __enter__ to check if it is a delete marker or add marker 
# TODO: create a context manager to benchmark the function -> requires better object oriented design pattern in here 

"""
from mmcv.ops import get_compiling_cuda_version, get_compiler_version
# Setup 

import mmdet
import mmcv
import torch, torchvision 

print("torch version: ",torch.__version__, "| torch cuda available: ",torch.cuda.is_available())
print("mmdetection version: ",mmdet.__version__)
print("mmcv version: ", mmcv.__version__)
print("compiling cuda version: ", get_compiling_cuda_version())
print("compiler version: ", get_compiler_version())

# from time import ctime  
# import time 
"""

# optimization: https://www.python.org/doc/essays/list2str/
