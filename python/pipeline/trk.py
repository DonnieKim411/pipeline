import datajoint as dj
import pandas as pd

from djaddon import hdf5

schema = dj.schema('pipeline_pupiltracking', locals())
from . import rf
import numpy as np
import os
import matplotlib.pyplot as plt


@schema
class VideoGroup(dj.Lookup):
    definition = """
    # table that groups videos into groups that can be tracked by the same SVM
    videogroup_id       : tinyint # id of the video group
    ---
    group_name          : char(20) # name of the group
    """

    contents = [  # these contents will be automatically inserted into the database
        (1, 'setup_S505')
    ]


@schema
class SVM(dj.Lookup):
    definition = """
    # table that stores the paths for the SVMs for each VideoGroup
    ->VideoGroup
    version         : int   # version of the SVM
    ---
    svm_path        : varchar(200) # path to the SVM file
    """

    contents = [
        (1, 0, 'no_SVM'),
        (1, 1, '/media/lab/users/jagrawal/global_svm/svm_version1/svm'),
        (1, 2, '/media/lab/users/jagrawal/global_svm/svm_version2/svm'),
        (1, 3, '/media/lab/users/jagrawal/global_svm/svm_version3/svm'),
    ]


@schema
class ROI(dj.Manual):
    definition = """
    # table that stores the correct ROI of the Eye in the video
    ->rf.Eye
    x_roi                     : int                         # x coordinate of roi
    y_roi                     : int                         # y coordinate of roi
    ---
    """


@schema
class EyeFrame(dj.Computed):
    definition = """
    # eye tracking info for each frame of a movie
    -> rf.Eye
    -> SVM
    frame                       : int                           # frame number in movie
    ---
    eye_frame_ts=CURRENT_TIMESTAMP    : timestamp               # automatic
    """

    @property
    def populated_from(self):
        # return rf.Eye() * SVM() * VideoGroup().aggregate(SVM(), current_version='MAX(version)') & 'version=current_version'
        return rf.Eye() * SVM() * VideoGroup().aggregate(SVM(), current_version='MAX(version)') & 'version=0'

    def _make_tuples(self, key):
        print("Entered make tuples")
        print(key)
        svm_path = (SVM() & key).fetch1['svm_path']
        print(svm_path)

        # x_roi=900
        # y_roi=420
        # pass
        # roi = (rf.Eye() & key).fetch1['eye_roi']
        # print(roi)
        patch_size = 350
        # y_roi = (roi[0][1] + roi[0][3]) / 2 - patch_size / 2
        # x_roi = (roi[0][0] + roi[0][2]) / 2 - patch_size / 2
        # x_roi = 275 - patch_size / 2
        # y_roi = 675 - patch_size / 2
        x_roi = (ROI() & key).fetch1['x_roi']
        y_roi = (ROI() & key).fetch1['y_roi']
        print("ROI used for video = ", x_roi, y_roi)
        efd = EyeFrame.Detection()

        # Code to do tracking
        from IPython import embed
        # embed()

        # print(key)
        kk = key['animal_id']
        si = key['scan_idx']
        # svm="/media/lab/users/jagrawal/global_svm/svm_version2/svm"
        # out = "/media/lab/users/jagrawal/global_svm/151123/m7199A9eyetracking/out"
        video = "m" + str(kk) + "A" + str(si) + "*"
        command = "find /media/scratch01/WholeCell/jake/* -name " + video + ".avi"
        print(command)
        video_path = os.popen(command).read()
        video_path = video_path.strip(' \n\t')
        # print("video_path=",video_path)
        folder = video_path.split("/")[5]
        print(folder)
        debug = 0
        if len(video_path) != 0:
            print("Found video and going for tracking")
            # if (os.path.exists(folder + "/" + video)) and debug == 0:
            #     print("Data already exists for " + folder + "/" + video)
            # else:
            # Delete if data already present and start tracking again
            command = "rm -rf " + folder
            os.system(command)
            print("Making directory: " + folder + "/" + video)
            command = "mkdir -p " + folder + "/" + video + "/images"
            if debug == 0:
                os.system(command)

                # if (svm_path.find('no_SVM') + 1):
                #     # print("if")
                # Path indicated below is for docker file
                command = "cd " + folder + "/" + video + "; python2 /data/pupil-tracking/track_without_SVM.py " + str(
                    int(x_roi)) + " " + str(int(y_roi)) + " " + video_path + " -P " + str(
                    int(patch_size)) + "; cd ../.."
            # else:
            # print("else")

            # command = "cd " + folder + "/" + video + "; python2 /data/Pupil-tracking/track.py " + out + " " + svm_path + " " + video_path + "; cd ../.."

            print("Running command :", command)
            if debug == 0:
                # print(command)
                os.system(command)

            # CODE to insert data after tracking
            print("Tracking complete... Now inserting data to datajoint")
            df = pd.read_csv(str(folder + '/' + video + "/trace.csv"))
            for index, data in df.iterrows():
                key['frame'] = index + 1
                self.insert1(key)
                if pd.notnull(data['pupil_x']):
                    values = data.to_dict()
                    values.update(key)
                    efd.insert1(values)

                    # efd.insert([e.to_dict() for _, e in df.iterrows()])

        else:
            print("Video not found")

    class Detection(dj.Part):
        definition = """
        # eye frames with detected eye
        ->EyeFrame
        ---
        pupil_x                     : float                         # pupil x position
        pupil_y                     : float                         # pupil y position
        pupil_r_minor               : float                         # pupil radius minor axis
        pupil_r_major               : float                         # pupil radius major axis
        pupil_angle                 : float                         # angle of major axis vs. horizontal axis in radians
        pupil_x_std                 : float                         # pupil x position std
        pupil_y_std                 : float                         # pupil y position std
        pupil_r_minor_std            : float                         # pupil radius minor axis std
        pupil_r_major_std           : float                         # pupil radius major axis std
        pupil_angle_std             : float                         # angle of major axis vs. horizontal axis in radians
        intensity_std               : float                         # standard deviation of the ROI pixel values
        """


@schema
class FilterProtocol(dj.Lookup):
    definition = """
    # groups of filtering steps to reject bad frames

    filter_protocol_id      : int   # id of the filtering protocol
    ---
    protocol_name           : char(50) # descriptive name of the protocol
    """

    contents = [
        {'filter_protocol_id': 0, 'protocol_name': 'frame_intensity'},
    ]

    def apply(self, frames, key):
        for step in (ProtocolStep() & key).fetch.order_by('priority').as_dict():
            frames = FrameFilter().apply(frames, step, param=step['filter_param'])
        return frames


@schema
class FrameFilter(dj.Lookup):
    definition = """
    # single filters to reject frames
    filter_id           : tinyint   # id of the filter
    ---
    filter_name         : char(50)   # descriptive name of the filter
    """

    contents = [
        {'filter_id': 0, 'filter_name': 'intensity_filter'},
    ]

    def apply(self, frames, key, param):
        """
        Apply takes a restriction of EyeFrame.Detection() and returns an even more restricted set of frames
        :param frames: restriction of EyeFrame.Detection()
        :param key: key that singles out a single filter
        :param param: parameters to the filter
        :return: an even more restricted set of frames
        """
        which = (self & key).fetch1['filter_name']

        if which == 'intensity_filter':
            i = frames.fetch['intensity_std']
            th = np.percentile(i, param) / 2
            return frames & 'intensity_std>{threshold}'.format(threshold=th)


@schema
class ProtocolStep(dj.Lookup):
    definition = """
    # single filter in a protocol to accept frames
    -> FilterProtocol
    -> FrameFilter
    priority                : int   # priority of the filter step, the low the higher the priority
    ---
    filter_param=null       : longblob # parameters that are passed to the filter
    """

    # define the protocols. Each protocol has one id, but can have several filters
    contents = [ # parameter needs to be an array
        # protocol 0 contains only one filter and is based on intensity
        {'filter_protocol_id': 0, 'filter_id': 0, 'priority': 50, 'filter_param': np.array(50)},
    ]


@schema
class FilteredFrame(dj.Computed):
    definition = """
    # This schema only contains detected frames that meet a particular quality criterion
    -> EyeFrame.Detection
    -> FilterProtocol
    """

    @property
    def populated_from(self):
        return rf.Eye() * FilterProtocol() & EyeFrame()

    def _make_tuples(self, key):
        print("Key = ", key)

        frames = EyeFrame.Detection() & key
        print('\tLength before filtering: {l}'.format(l=len(frames)))
        frames = (FilterProtocol() & key).apply(frames, key)
        print('\tLength after filtering: {l}'.format(l=len(frames)))

        # TODO: move the filters up to FrameFilter
        # i = (EyeFrame.Detection() & key).fetch['pupil_x']
        # rejected_spikes = np.where(abs(i - np.mean(i) > 10 * np.std(i)))

        rejected_ransac_x = np.asarray([])
        # i = (EyeFrame.Detection() & key).fetch['pupil_x_std']
        # rejected_ransac_x = np.where(i > 1)

        rejected_ransac_y = np.asarray([])
        # i = (EyeFrame.Detection() & key).fetch['pupil_y_std']
        # rejected_ransac_y = np.where(i >i)
        # embed()
        # rej = np.unique(np.concatenate([rejected_intensity[0], rejected_spikes[0]]))

        # remove these indexes and get the valid frames
        # change the decision parameter video per video basis

        for frame_key in frames.project().fetch.as_dict:
            key.update(frame_key)
            self.insert1(key)



            # rejected_noise = []
            # for frame_key in (EyeFrame.Detection() & key).project().fetch.as_dict:
            #     #embed()
            #     if int(frame_key['frame']) is 1:
            #         last_pos = (EyeFrame.Detection() & frame_key).fetch['pupil_x']
            #     else:
            #         pos = (EyeFrame.Detection() & frame_key).fetch['pupil_x']
            #         motion = pos - last_pos
            #         if abs(motion) < 60:
            #             last_pos = pos
            #         else:
            #             rejected_noise.append(int(frame_key['frame']))
            #             #print(rejected_noise)
            #             # if index == 7227:
            #             # embed()
            #             last_pos += 25 * np.sign(motion)
            #             print(rejected_noise)
            # embed()





            # x = EyeFrame.Detection().fetch['pupil_x']
            # for index, data in enumerate(x):
            #     embed()


@schema
class Quality(dj.Computed):
    definition = """
    # quality assessment of tracking using Jake's tracked frames as ground truth
    -> rf.Eye
    ---
    pos_err       : float # mean Euclidean distance between pupil positions
    r_corr         : float # correlation of radii
    excess_frames : int   # number of frames detected by tracking but not in Jake's data
    missed_frames : int   # number of frames detected by Jake but no by tracking
    total_frames  : int   # total number of frames in the video
    """

    @property
    def populated_from(self):
        return rf.Eye().project() & EyeFrame().project() & rf.EyeFrame().project() & FilteredFrame().project()

    def _make_tuples(self, key):
        # TODO: This function needs cleanup. Only keep relevant stuff for computing the comparisons
        # TODO: Don't plot in _make_tuples. Make plotting an extra function.

        roi_rf = (rf.Eye() & key).fetch['eye_roi']

        from IPython import embed
        # embed()
        print("Populating for key= ", key)
        pos_errors = np.zeros(len(rf.EyeFrame() & key))
        r_errors = np.zeros(len(rf.EyeFrame() & key))
        excess_frames = 0
        missed_frames = 0
        r_rf = []
        r_trk = []
        total_frames = len(rf.EyeFrame() & key)
        for frame_key in (rf.EyeFrame() & key).project().fetch.as_dict:

            # from IPython import embed
            # embed()
            if np.isnan((rf.EyeFrame() & frame_key).fetch['pupil_x']):
                if (EyeFrame.Detection() & frame_key).fetch['pupil_x'].shape[0] != 0:
                    excess_frames += 1
            else:
                if (EyeFrame.Detection() & frame_key & FilteredFrame()).fetch['pupil_x'].shape[0] == 0:
                    missed_frames += 1
                else:
                    threshold = 1.2
                    threshold = 10
                    if (EyeFrame.Detection() & frame_key).fetch1['pupil_x_std'] > threshold or \
                                    (EyeFrame.Detection() & frame_key).fetch1['pupil_y_std'] > threshold:
                        missed_frames += 1
                    else:
                        d_x = (rf.EyeFrame() & frame_key).fetch['pupil_x'][0] - \
                              (EyeFrame.Detection() & frame_key).fetch['pupil_x'][0] + roi_rf[0][0][0] - 2
                        d_y = (rf.EyeFrame() & frame_key).fetch['pupil_y'][0] - \
                              (EyeFrame.Detection() & frame_key).fetch['pupil_y'][0] + roi_rf[0][0][2] - 2
                        # r_errors[frame_key['frame']] = (rf.EyeFrame() & frame_key).fetch['pupil_r'][0] - \
                        #                              (EyeFrame.Detection() & frame_key).fetch['pupil_r_major'][
                        #                                   0]
                        r_rf.append((rf.EyeFrame() & frame_key).fetch['pupil_r'][0])
                        r_trk.append((EyeFrame.Detection() & frame_key).fetch['pupil_r_major'][0])
                        pos_errors[frame_key['frame']] = pow(d_x, 2) + pow(d_y, 2)
                        if frame_key['frame'] % 1000 is 0:
                            print("Frame Computing = ", frame_key['frame'], " / ", total_frames)
        key['pos_err'] = pow(np.mean(pos_errors), 0.5)
        key['r_corr'] = np.corrcoef(r_rf, r_trk)[0][1]
        key['excess_frames'] = excess_frames
        key['missed_frames'] = missed_frames
        key['total_frames'] = total_frames
        # embed()
        self.insert1(key)

    def plot_comparison(self, key):
        pass
        # TODO: Make this a proper plotting function
        # fig, ax = plt.subplots(3, 1, sharex=True)
        # r_rf = (rf.EyeFrame() & key).fetch['pupil_r']
        # r_trk = (EyeFrame.Detection() & key).fetch['pupil_r_major']
        # ax[0].plot(r_rf)
        # ax[1].plot(r_trk)
        # ax[2].plot(r_errors)
        # fig.savefig('error_radius.png')
        #
        # fig, ax = plt.subplots(3, 1, sharex=True)
        # r_rf = (rf.EyeFrame() & key).fetch['pupil_x']
        # r_trk = (EyeFrame.Detection() & key).fetch['pupil_x']
        # ax[0].set_ylim([np.nanmean(r_rf) - 25, np.nanmean(r_rf) + 25])
        # ax[1].set_ylim([np.nanmean(r_rf) - 25, np.nanmean(r_rf) + 25])
        # ax[2].set_ylim([0, 100])
        # ax[0].plot(r_rf)
        # ax[1].plot(r_trk - roi_rf[0][0][0])
        # ax[2].plot(pos_errors)
        # fig.savefig('error_pupil_x.png')
        # # ax[2].plot()

# from microns.trk import EyeFrame
# EyeFrame().populate(restriction=dict(animal_id=2055, group_name='setup_jake'))
