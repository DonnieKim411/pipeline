import os
from warnings import warn
import datajoint as dj
import numpy as np
import sh
from commons import lab
try:
    import pyfnnd
except ImportError:
    warn('Could not load pyfnnd.  Oopsi spike inference will fail. Install from https://github.com/cajal/PyFNND.git')

from . import experiment, config, PipelineException
from .utils.dsp import mirrconv
from .utils.eye_tracking import ROIGrabber, ts2sec, read_video_hdf5, CVROIGrabber
from .utils import galvo_corrections
import matplotlib.pyplot as plt


from distutils.version import StrictVersion
assert StrictVersion(dj.__version__) >= StrictVersion('0.2.9')

schema = dj.schema('pipeline_preprocess', locals())


def notnan(x, start=0, increment=1):
    while np.isnan(x[start]) and 0 <= start < len(x):
        start += increment
    return start


def fill_nans(x):
    """
    :param x:  1D array  -- will
    :return: the array with nans interpolated
    The input argument is modified.
    """
    nans = np.isnan(x)
    x[nans] = 0 if nans.all() else np.interp(nans.nonzero()[0], (~nans).nonzero()[0], x[~nans])
    return x


def normalize(img):
    return (img - img.min()) / (img.max() - img.min())


def erd():
    """a shortcut for convenience"""
    dj.ERD(schema).draw(prefix=False)


@schema
class Slice(dj.Lookup):
    definition = """  # slices in resonant scanner scans
    slice  : tinyint  # slice in scan
    """
    contents = ((i,) for i in range(12))


@schema
class Channel(dj.Lookup):
    definition = """  # recording channel, directly related to experiment.PMTFilterSet.Channel
    channel : tinyint
    """
    contents = [[1], [2], [3], [4]]


@schema
class Prepare(dj.Imported):
    definition = """  # master table that gathers data about the scans of different types, prepares for trace extraction
    -> experiment.Scan
    """

    class Galvo(dj.Part):
        definition = """    # basic information about resonant microscope scans, raster correction
        -> Prepare
        ---
        nframes_requested       : int               # number of valumes (from header)
        nframes                 : int               # frames recorded
        px_width                : smallint          # pixels per line
        px_height               : smallint          # lines per frame
        um_width                : float             # width in microns
        um_height               : float             # height in microns
        bidirectional           : tinyint           # 1=bidirectional scanning
        fps                     : float             # (Hz) frames per second
        zoom                    : decimal(4,1)      # zoom factor
        dwell_time              : float             # (us) microseconds per pixel per frame
        nchannels               : tinyint           # number of recorded channels
        nslices                 : tinyint           # number of slices
        slice_pitch             : float             # (um) distance between slices
        fill_fraction           : float             # raster scan fill fraction (see scanimage)
        preview_frame           : longblob          # raw average frame from channel 1 from an early fragment of the movie
        raster_phase            : float             # shift of odd vs even raster lines
        """

        def get_correct_raster(self):
            """
             :returns: A function to perform raster correction on the scan
                    [image_height, image_width, channels, slices, num_frames].
            """
            raster_phase, fill_fraction = self.fetch1['raster_phase', 'fill_fraction']
            if raster_phase == 0:
                return lambda scan: np.double(scan)
            else:
                return lambda scan: galvo_corrections.correct_raster(scan, raster_phase,
                                                                     fill_fraction)

        def estimate_num_components_per_slice(self):
            """ Estimates the number of components per scan slice using simple rules of thumb.

            For somatic scans, estimate number of neurons based on:
            (100x100x100)um^3 = 1e6 um^3 -> 1e2 neurons; (1x1x1)mm^3 = 1e9 um^3 -> 1e5 neurons

            For axonal/dendritic scans, just ten times our estimate of neurons.

            :returns: Number of components
            :rtype: int
            """

            # Get slice dimensions (in micrometers)
            slice_height, slice_width = (Prepare.Galvo() & self).fetch1['um_height',
                                                                       'um_width']
            slice_thickness = 10  # assumption
            slice_volume = slice_width * slice_height * slice_thickness

            # Estimate number of components
            if experiment.Session.TargetStructure() & self:  # scan is axonal/dendritic
                num_components = slice_volume * 0.001  # ten times as many neurons
            else:
                num_components = slice_volume * 0.0001

            return int(round(num_components))

        def estimate_neuron_size_in_pixels(self):
            """ Estimates the size of a neuron in the scan (in pixels). Assumes neurons
             are 15 x 15 microns."""
            neuron_size_in_microns = 15 # assumption

            # Calculate size in pixels
            um_width, px_width = (Prepare.Galvo() & self).fetch1['um_width', 'px_width']
            one_micron_in_pixels = px_width / um_width
            neuron_size_in_pixels = neuron_size_in_microns * one_micron_in_pixels

            # Make sure it is int and at least 1.
            neuron_size_in_pixels = int(max(round(neuron_size_in_pixels), 1))

            return neuron_size_in_pixels


    class GalvoMotion(dj.Part):
        definition = """   # motion correction for galvo scans
        -> Prepare.Galvo
        -> Slice
        ---
        -> Channel
        template                    : longblob       # stack that was used as alignment template
        motion_xy                   : longblob       # (pixels) y,x motion correction offsets
        motion_rms                  : float          # (um) stdev of motion
        align_times=CURRENT_TIMESTAMP: timestamp     # automatic
        """

        def get_correct_motion(self):
            """
            :returns: A function to performs motion correction on scans
                      [image_height, image_width, channels, slices, num_frames].
            """
            xy_motion = self.fetch1['motion_xy']

            def my_lambda_function(scan, indices=None):
                if indices is None:
                    return galvo_corrections.correct_motion(scan, xy_motion)
                else:
                    return galvo_corrections.correct_motion(scan, xy_motion[:, indices])

            return my_lambda_function

    class GalvoAverageFrame(dj.Part):
        definition = """   # average frame for each slice and channel after corrections
        -> Prepare.GalvoMotion
        -> Channel
        ---
        frame  : longblob     # average frame ater Anscombe, max-weighting,
        """

    class Aod(dj.Part):
        definition = """   # information about AOD scans
        -> Prepare
        """

    class AodPoint(dj.Part):
        definition = """  # points in 3D space in coordinates of an AOD scan
        -> Prepare.Aod
        point_id : smallint    # id of a scan point
        ---
        x: float   # (um)
        y: float   # (um)
        z: float   # (um)
        """

    def save_video(self, filename='galvo_corrections.mp4', slice=1, channel=1,
                   start_index=0, seconds=30, dpi=200):
        """ Creates an animation video showing the original vs corrected scan.

        :param string filename: Output filename (path + filename)
        :param int slice: Slice to use for plotting (key for GalvoMotion). Starts at 1
        :param int channel: What channel from the scan to use. Starts at 1
        :param int start_index: Where in the scan to start the video.
        :param int seconds: How long in seconds should the animation run.
        :param int dpi: Dots per inch, controls the quality of the video.

        :returns Figure. You can call show() on it.
        :rtype: matplotlib.figure.Figure
        """
        # Get scan filename
        scan_filename = (experiment.Scan() & self).local_filenames_as_wildcard

        # Get fps and total_num_frames
        fps = (Prepare.Galvo() & self).fetch1['fps']
        num_video_frames = int(round(fps * seconds))
        stop_index = start_index + num_video_frames

        # Load the scan
        from tiffreader import TIFFReader
        reader = TIFFReader(scan_filename)
        scan = np.double(reader[:, :, channel - 1, slice - 1, start_index: stop_index])
        scan = scan.squeeze()
        original_scan = scan.copy()

        # Correct the scan
        correct_motion = (Prepare.GalvoMotion() & self &
                          {'slice': slice, 'channel': channel}).get_correct_motion()
        correct_raster = (Prepare.Galvo() & self).get_correct_raster()
        raster_corrected = correct_raster(scan)
        motion_corrected = correct_motion(raster_corrected, range(start_index, stop_index))
        corrected_scan = motion_corrected

        # Create animation
        import matplotlib.animation as animation

        ## Set the figure
        fig = plt.figure()

        plt.subplot(1, 2, 1)
        plt.title('Original')
        im1 = plt.imshow(original_scan[:, :, 0], vmin=original_scan.min(),
                         vmax=original_scan.max())  # just a placeholder
        plt.axis('off')
        plt.colorbar()

        plt.subplot(1, 2, 2)
        plt.title('Corrected')
        im2 = plt.imshow(corrected_scan[:, :, 0], vmin=corrected_scan.min(),
                         vmax=corrected_scan.max())  # just a placeholder
        plt.axis('off')
        plt.colorbar()

        ## Make the animation
        def update_img(i):
            im1.set_data(original_scan[:, :, i])
            im2.set_data(corrected_scan[:, :, i])

        video = animation.FuncAnimation(fig, update_img, num_video_frames,
                                        interval=1000 / fps)

        # Save animation
        print('Saving video at:', filename)
        print('If this takes too long, stop it and call again with dpi < 200 (default)')
        video.save(filename, dpi=dpi)

        return fig


@schema
class Method(dj.Lookup):
    definition = """  #  methods for extraction from raw data for either AOD or Galvo data
    extract_method :  tinyint
    """
    contents = [[1], [2], [3], [4]]

    class Aod(dj.Part):
        definition = """
        -> Method
        ---
        description  : varchar(60)
        high_pass_stop=null : float   # (Hz)
        low_pass_stop=null  : float   # (Hz)
        subtracted_princ_comps :  tinyint  # number of principal components to subtract
        """
        contents = [
            [1, 'raw traces', None, None, 0],
            [2, 'band pass, -1 princ comp', 0.02, 20, -1],
        ]

    class Galvo(dj.Part):
        definition = """  # extraction methods for galvo
        -> Method
        ---
        segmentation  :  varchar(16)   #
        """
        contents = [
            [1, 'manual'],
            [2, 'nmf']
        ]


@schema
class ExtractRaw(dj.Imported):
    definition = """
    # Correction, source extraction and trace deconvolution of a two-photon scan
    -> Prepare
    -> Method
    """
    @property
    def key_source(self):
        return Prepare() * Method() & dj.OrList(
            [(Prepare.Galvo() * Method.Galvo() - 'segmentation="manual"'),
             Prepare.Galvo() * Method.Galvo() * ManualSegment(),
             Prepare.Aod() * Method.Aod()] )

    class Trace(dj.Part):
        definition = """
        # Raw trace, common to Galvo
        -> ExtractRaw
        -> Channel
        trace_id  : smallint
        ---
        raw_trace : longblob     # unprocessed calcium trace
        """

    class GalvoSegmentation(dj.Part):
        definition = """
        # Segmentation of galvo movies
        -> ExtractRaw
        -> Slice
        ---
        segmentation_mask=null  :  longblob
        """

    class GalvoROI(dj.Part):
        definition = """
        # Region of interest produced by segmentation
        -> ExtractRaw.GalvoSegmentation
        -> ExtractRaw.Trace
        ---
        mask_pixels          :longblob      # indices into the image in column major (Fortran) order
        mask_weights = null  :longblob      # weights of the mask at the indices above
        """

        @staticmethod
        def reshape_masks(mask_pixels, mask_weights, px_height, px_width):
            ret = np.zeros((px_height, px_width, len(mask_pixels)))
            for i, (mp, mw) in enumerate(zip(mask_pixels, mask_weights)):
                mask = np.zeros(px_height * px_width)
                mask[mp.squeeze().astype(int) - 1] = mw.squeeze()
                ret[..., i] = mask.reshape(px_height, px_width, order='F')
            return ret

        def get_mask_as_image(self):
            """Return the mask for this single ROI as  an image (2-d array)"""
            # Get params
            pixel_indices, weights = (ExtractRaw.GalvoROI() & self).fetch1['mask_pixels',
                                                                           'mask_weights']
            image_height, image_width = (Prepare.Galvo() & self).fetch1['px_height',
                                                                        'px_width']
            # Calculate and reshape mask
            mask_as_vector = np.zeros(image_height * image_width)
            mask_as_vector[pixel_indices - 1] = weights
            spatial_mask = mask_as_vector.reshape(image_height, image_width, order='F')

            return spatial_mask

    class SpikeRate(dj.Part):
        definition = """
        # Spike trace deconvolved during CNMF
        -> ExtractRaw.Trace
        ---
        spike_trace :longblob
        """

    class GalvoCorrelationImage(dj.Part):
        definition = """
        # Each pixel shows the (average) temporal correlation between that pixel and its eight neighbors
        -> ExtractRaw
        -> Channel
        -> Slice
        ---
        correlation_image   : longblob # correlation image
        """

    class BackgroundComponents(dj.Part):
        definition = """
        # Inferred background components with the CNMF algorithm
        -> ExtractRaw
        -> Channel
        -> Slice
        ----------------
        masks    : longblob # array (im_width x im_height x num_background_components)
        activity : longblob # array (num_background_components x timesteps)
        """

    class ARCoefficients(dj.Part):
        definition = """
        # Fitted parameters for the autoregressive process (CNMF)
        -> ExtractRaw.Trace
        ----------------
        g: longblob # array with g1, g2, ... values for the AR process
        """

    class CNMFParameters(dj.Part):
        definition = """
        # Arguments used to demix and deconvolve the scan with CNMF
        -> ExtractRaw
        --------------
        num_components  : smallint # estimated number of components
        ar_order        : tinyint # order of the autoregressive process for impulse function response
        merge_threshold : float # overlapping masks are merged if temporal correlation greater than this
        num_processes = null    : smallint # number of processes to run in parallel, null=all available
        num_pixels_per_process  : int # number of pixels processed at a time
        block_size      : int # number of pixels per each dot product
        init_method     : enum("greedy_roi", "sparse_nmf") # type of initialization used
        neuron_size_in_pixels = null :   tinyint
        snmf_alpha = null       : float   # Regularization parameter for SNMF
        num_background_components : smallint # estimated number of background components
        init_on_patches         : boolean   # whether to run initialization on small patches
        patch_downsampling_factor = null : tinyint # how to downsample the scan
        percentage_of_patch_overlap = null : float # overlap between adjacent patches
        """

    def plot_traces_and_masks(self, traces, slice, mask_channel=1, outfile='traces.pdf'):

        import seaborn as sns

        key = (self * self.GalvoSegmentation().proj() * Method.Galvo() & dict(segmentation='nmf', slice=slice))
        trace_selection = 'trace_id in ({})'.format(','.join([str(s) for s in traces]))
        rel = self.GalvoROI() * self.SpikeRate() * ComputeTraces.Trace() & key & dict(segmentation=2) & trace_selection

        mask_px, mask_w, spikes, traces, ids \
            = rel.fetch.order_by('trace_id')['mask_pixels', 'mask_weights', 'spike_trace', 'trace', 'trace_id']
        template = np.stack((normalize(t) for t in (Prepare.GalvoAverageFrame() & key).fetch['frame'])
                            , axis=2)[..., mask_channel - 1]

        d1, d2, fps = [int(elem) for elem in (Prepare.Galvo() & key).fetch1['px_height', 'px_width', 'fps']]
        selected_window = int(np.round(fps * 120))
        t = np.arange(selected_window) / fps

        masks = self.GalvoROI.reshape_masks(mask_px, mask_w, d1, d2)

        plot_grid = plt.GridSpec(1, 3)

        with sns.axes_style('white'):
            fig = plt.figure(figsize=(15, 5), dpi=100)
            ax_image = fig.add_subplot(plot_grid[0, 0])
        with sns.axes_style('ticks'):
            ax = fig.add_subplot(plot_grid[0, 1:])

        ax_image.imshow(template, cmap=plt.cm.gray)
        spike_traces = np.hstack(spikes).T
        # --- plot zoom in
        T = spike_traces.shape[1]
        spike_traces[np.isnan(spike_traces)] = 0
        loc = np.argmax(np.convolve(spike_traces.sum(axis=0), np.ones(selected_window) / selected_window, mode='same'))
        loc = max(loc - selected_window // 2, 0)
        loc = T - selected_window if loc > T - selected_window else loc

        offset = 0
        for i, (ca_trace, trace_id) in enumerate(zip(traces, ids)):
            ca_trace = np.array(ca_trace[loc:loc + selected_window])
            ca_trace -= ca_trace.min()
            ax.plot(t, ca_trace + offset, 'k', lw=1)
            offset += ca_trace.max() * 1.1
            tmp_mask = np.asarray(masks[..., i])
            tmp_mask[tmp_mask == 0] = np.NaN
            ax_image.imshow(tmp_mask, cmap=plt.cm.get_cmap('autumn'), zorder=10, alpha=.5)
            fig.suptitle(
                "animal {animal_id} session {session} scan {scan_idx} slice {slice}".format(
                    trace_id=trace_id, **key.fetch1()))
        ax.set_yticks([])
        ax.set_ylabel('Fluorescence [a.u.]')
        ax.set_xlabel('time [s]')
        sns.despine(fig, left=True)
        fig.savefig(outfile)

    def plot_galvo_ROIs(self, outdir='./'):
        import seaborn as sns

        sns.set_context('paper')
        theCM = sns.blend_palette(['lime', 'gold', 'deeppink'], n_colors=10)  # plt.cm.RdBu_r
        # theCM = plt.cm.get_cmap('viridis')

        for key in (self * self.GalvoSegmentation().proj() * Method.Galvo() & dict(segmentation='nmf')).fetch.as_dict:
            mask_px, mask_w, spikes, traces, ids = (
                self.GalvoROI() * self.SpikeRate() *
                ComputeTraces.Trace() & key & dict(segmentation=2)).fetch.order_by('trace_id')[
                'mask_pixels', 'mask_weights', 'spike_trace', 'trace', 'trace_id']

            template = np.stack([normalize(t)
                                 for t in (Prepare.GalvoAverageFrame() & key).fetch['frame']], axis=2).max(axis=2)

            d1, d2, fps = tuple(map(int, (Prepare.Galvo() & key).fetch1['px_height', 'px_width', 'fps']))
            hs = int(np.round(fps * 60))
            masks = self.GalvoROI.reshape_masks(mask_px, mask_w, d1, d2)
            try:
                sh.mkdir('-p', os.path.expanduser(outdir) + '/scan_idx{scan_idx}/slice{slice}'.format(**key))
            except:
                pass
            gs = plt.GridSpec(6, 2)

            N = len(spikes)
            for cell, (sp_trace, ca_trace, trace_id) in enumerate(zip(spikes, traces, ids)):
                print(
                    "{trace_id:03d}/{N}: animal_id {animal_id}\tsession {session}\tscan_idx {scan_idx:02d}\t{segmentation}\tslice {slice}".format(
                        trace_id=trace_id, N=N, **key))
                sp_trace = sp_trace.squeeze()
                ca_trace = ca_trace.squeeze()
                with sns.axes_style('white'):
                    fig = plt.figure(figsize=(9, 12), dpi=400)
                    ax_image = fig.add_subplot(gs[:-2, 0])

                with sns.axes_style('ticks'):
                    ax_small_tr = fig.add_subplot(gs[1, 1])
                    ax_small_ca = fig.add_subplot(gs[2, 1], sharex=ax_small_tr)
                    ax_sp = fig.add_subplot(gs[-1, :])
                    ax_tr = fig.add_subplot(gs[-2, :], sharex=ax_sp)

                # --- plot zoom in
                n = len(sp_trace)
                tmp = np.array(sp_trace)
                tmp[np.isnan(tmp)] = 0
                loc = np.argmax(np.convolve(tmp, np.ones(hs) / hs, mode='same'))
                loc = max(loc - hs // 2, 0)
                loc = n - hs if loc > n - hs else loc

                ax_small_tr.plot(sp_trace[loc:loc + hs], 'k', lw=1)
                ax_small_ca.plot(ca_trace[loc:loc + hs], 'k', lw=1)

                # --- plot traces
                ax_sp.plot(sp_trace, 'k', lw=1)

                ax_sp.fill_between([loc, loc + hs], np.zeros(2), np.ones(2) * np.nanmax(sp_trace),
                                   color='steelblue', alpha=0.5)
                ax_tr.plot(ca_trace, 'k', lw=1)
                ax_tr.fill_between([loc, loc + hs], np.zeros(2), np.ones(2) * np.nanmax(ca_trace),
                                   color='steelblue', alpha=0.5)
                ax_image.imshow(template, cmap=plt.cm.gray)
                # ax_image.contour(masks[..., cell], colors=theCM, zorder=10)
                tmp_mask = np.asarray(masks[..., cell])
                tmp_mask[tmp_mask == 0] = np.NaN
                ax_image.imshow(tmp_mask, cmap=plt.cm.get_cmap('autumn'), zorder=10, alpha=.3)

                fig.suptitle(
                    "animal_id {animal_id}:session {session}:scan_idx {scan_idx}:{segmentation}:slice{slice}:trace_id{trace_id}".format(
                        trace_id=trace_id, **key))

                sns.despine(fig)
                ax_sp.set_title('NMF spike trace', fontweight='bold')
                ax_tr.set_title('Raw trace', fontweight='bold')
                ax_small_tr.set_title('NMF spike trace', fontweight='bold')
                ax_small_ca.set_title('Raw trace', fontweight='bold')
                ax_sp.axis('tight')
                for a in [ax_small_ca, ax_small_tr, ax_sp, ax_tr]:
                    a.set_xticks([])
                    a.set_yticks([])
                    sns.despine(ax=a, left=True)
                ax_sp.set_xlabel('time')
                fig.tight_layout()
                plt.savefig(
                    outdir + "/scan_idx{scan_idx}/slice{slice}/trace_id{trace_id:03d}_animal_id_{animal_id}_session_{session}.png".format(
                        trace_id=trace_id, **key))
                plt.close(fig)

    def _make_tuples(self, key):
        """ Load scan one slice & channel at a time, correct for raster and motion
        artifacts and use CNMF to extract sources and deconvolve spike traces.

        See caiman_interface.demix_and_deconvolve_with_cnmf for an explanation of params
        """
        from .utils import caiman_interface as cmn

        print('-'*50)
        print('ExtractRaw: Processing scan {}'.format(key))

        # Insert key in ExtractRaw
        self.insert1(key)

        # Get scan filename
        scan_filename = (experiment.Scan() & key).local_filenames_as_wildcard

        # Read the scan
        from tiffreader import TIFFReader
        reader = TIFFReader(scan_filename)

        # Estimate number of components per slice
        num_components = (Prepare.Galvo() & key).estimate_num_components_per_slice()
        num_components += int(0.5 * num_components) # add 50% more just to be sure

        # Estimate the size of a neuron in the scan (used for somatic only)
        neuron_size_in_pixels = (Prepare.Galvo() & key).estimate_neuron_size_in_pixels()

        # Set general parameters
        kwargs = {}
        kwargs['num_components'] = num_components
        kwargs['AR_order'] = 2  # impulse response modelling with AR(2) process
        kwargs['merge_threshold'] = 0.8

        # Set performance/execution parameters (heuristically)
        kwargs['num_processes'] = 20  # Set to None for all cores available
        kwargs['num_pixels_per_process'] = 10000
        kwargs['block_size'] = 5000

        # Set params specific to somatic or axonal/dendritic scans
        is_somatic = not (experiment.Session.TargetStructure() & key)
        if is_somatic:
            kwargs['init_method'] = 'greedy_roi'
            kwargs['neuron_size_in_pixels'] = neuron_size_in_pixels
            kwargs['num_background_components'] = 4
            kwargs['init_on_patches'] = False
        else:
            kwargs['init_method'] = 'sparse_nmf'
            kwargs['snmf_alpha'] = 500  # 10^2 to 10^3.5 is a good range
            kwargs['num_background_components'] = 1
            kwargs['init_on_patches'] = True

        # Set params specific to initialization on patches
        if kwargs['init_on_patches']:
            kwargs['patch_downsampling_factor'] = 4
            kwargs['percentage_of_patch_overlap'] = .2

        # Over each channel and slice in the scan
        scan_slices, scan_channels = (Prepare.GalvoMotion() & key).fetch['slice', 'channel']
        for channel in scan_channels:
            num_traces_until_now = 0 # count traces over one channel

            for slice in scan_slices:
                # Load the scan
                scan = np.double(reader[:, :, channel - 1, slice - 1, :]).squeeze()

                # Correct scan
                correct_motion = (Prepare.GalvoMotion() & key &
                                  {'slice': slice, 'channel': channel}).get_correct_motion()
                correct_raster = (Prepare.Galvo() & key).get_correct_raster()
                corrected_scan = correct_motion(correct_raster(scan))

                # Compute and insert correlation image
                correlation_image = cmn.compute_correlation_image(corrected_scan)
                ExtractRaw.GalvoCorrelationImage().insert1({**key, 'slice': slice,
                                                            'channel': channel,
                                                            'correlation_image': correlation_image})

                # Extract traces
                cnmf_result = cmn.demix_and_deconvolve_with_cnmf(corrected_scan, **kwargs)
                (location_matrix, activity_matrix, background_location_matrix,
                background_activity_matrix, raw_traces, spikes, AR_params) = cnmf_result

                # Insert traces, spikes and spatial masks
                final_num_components = raw_traces.shape[0]
                for i in range(final_num_components):
                    # Create new trace key
                    trace_id = num_traces_until_now + i + 1
                    trace_key = {**key, 'trace_id': trace_id, 'channel': channel}

                    # Insert traces and spikes
                    ExtractRaw.Trace().insert1({**trace_key, 'raw_trace': raw_traces[i, :]})
                    ExtractRaw.SpikeRate().insert1({**trace_key, 'spike_trace': spikes[i, :]})

                    # Insert fitted AR parameters
                    if kwargs['AR_order'] > 0:
                        ExtractRaw.ARCoefficients().insert1({**trace_key, 'g': AR_params[i, :]})

                    # Get indices and weights of defined pixels in mask (matlab-like)
                    mask_as_F_ordered_vector = location_matrix[:, :, i].ravel(order='F')
                    defined_mask_indices = np.where(mask_as_F_ordered_vector)[0]
                    defined_mask_weights = mask_as_F_ordered_vector[defined_mask_indices]
                    defined_mask_indices += 1 # matlab indices start at 1

                    # Insert spatial mask
                    ExtractRaw.GalvoSegmentation().insert1({**key, 'slice': slice},
                                                           skip_duplicates=True)
                    ExtractRaw.GalvoROI().insert1({**trace_key, 'slice': slice,
                                                   'mask_pixels': defined_mask_indices,
                                                   'mask_weights': defined_mask_weights})
                num_traces_until_now += final_num_components # increase trace count

                # Insert background components
                background_dict = {**key, 'channel': channel, 'slice': slice,
                                   'masks': background_location_matrix,
                                   'activity': background_activity_matrix}
                ExtractRaw.BackgroundComponents().insert1(background_dict)

        # Insert CNMF parameters (one per scan)
        lowercase_kwargs = {key.lower(): value for key, value in kwargs.items()}
        ExtractRaw.CNMFParameters().insert1({**key, **lowercase_kwargs})

    def save_video(self, filename='cnmf_extraction.mp4', slice=1, channel=1,
                   start_index=0, seconds=30, dpi=200):
        """ Creates an animation video showing the original vs corrected scan.

        :param string filename: Output filename (path + filename)
        :param int slice: Slice to use for plotting. Starts at 1
        :param int channel: What channel from the scan to use. Starts at 1
        :param int start_index: Where in the scan to start the video.
        :param int seconds: How long in seconds should the animation run.
        :param int dpi: Dots per inch, controls the quality of the video.

        :returns Figure. You can call show() on it.
        :rtype: matplotlib.figure.Figure
        """
        # Get scan filename
        scan_filename = (experiment.Scan() & self).local_filenames_as_wildcard

        # Get fps and calculate total number of frames
        fps = (Prepare.Galvo() & self).fetch1['fps']
        num_video_frames = int(round(fps * seconds))
        stop_index = start_index + num_video_frames

        # Load the scan
        from tiffreader import TIFFReader
        reader = TIFFReader(scan_filename)
        scan = np.double(reader[:, :, channel - 1, slice - 1, start_index: stop_index])
        scan = scan.squeeze()

        # Correct the scan
        correct_motion = (Prepare.GalvoMotion() & self &
                          {'slice': slice, 'channel': channel}).get_correct_motion()
        correct_raster = (Prepare.Galvo() & self).get_correct_raster()
        raster_corrected = correct_raster(scan)
        motion_corrected = correct_motion(raster_corrected, range(start_index, stop_index))
        scan = motion_corrected

        # Get scan dimensions
        image_height, image_width, _ = scan.shape
        num_pixels = image_height * image_width

        # Get location and activity matrices
        location_matrix = self.get_all_masks(slice, channel)
        activity_matrix = self.get_all_traces(slice, channel)
        background_rel = ExtractRaw.BackgroundComponents() & self & {'slice': slice,
                                                                     'channel': channel}
        background_location_matrix, background_activity_matrix = \
            background_rel.fetch1['masks', 'activity']

        # Restrict computations to the necessary video frames
        activity_matrix = activity_matrix[:, start_index: stop_index]
        background_activity_matrix = background_activity_matrix[:, start_index: stop_index]

        # Calculate matrices
        extracted = np.dot(location_matrix.reshape(num_pixels, -1), activity_matrix)
        extracted = extracted.reshape(image_height, image_width, -1)
        background = np.dot(background_location_matrix.reshape(num_pixels, -1),
                            background_activity_matrix)
        background = background.reshape(image_height, image_width, -1)
        residual = scan - extracted - background

        # Create animation
        import matplotlib.animation as animation

        ## Set the figure
        fig = plt.figure()

        plt.subplot(2, 2, 1)
        plt.title('Original (Y)')
        im1 = plt.imshow(scan[:, :, 0], vmin=scan.min(), vmax=scan.max())  # just a placeholder
        plt.axis('off')
        plt.colorbar()

        plt.subplot(2, 2, 2)
        plt.title('Extracted (A*C)')
        im2 = plt.imshow(extracted[:, :, 0], vmin=extracted.min(), vmax=extracted.max())
        plt.axis('off')
        plt.colorbar()

        plt.subplot(2, 2, 3)
        plt.title('Background (B*F)')
        im3 = plt.imshow(background[:, :, 0], vmin=background.min(),
                         vmax=background.max())
        plt.axis('off')
        plt.colorbar()

        plt.subplot(2, 2, 4)
        plt.title('Residual (Y - A*C - B*F)')
        im4 = plt.imshow(residual[:, :, 0], vmin=residual.min(), vmax=residual.max())
        plt.axis('off')
        plt.colorbar()

        ## Make the animation
        def update_img(i):
            im1.set_data(scan[:, :, i])
            im2.set_data(extracted[:, :, i])
            im3.set_data(background[:, :, i])
            im4.set_data(residual[:, :, i])

        video = animation.FuncAnimation(fig, update_img, num_video_frames,
                                        interval=1000 / fps)

        # Save animation
        print('Saving video at:', filename)
        print('If this takes too long, stop it and call again with dpi < 200 (default)')
        video.save(filename, dpi=dpi)

        return fig

    def plot_contours(self, slice=1, channel=1):
        """ Draw contours of all masks over the correlation image.

        :param slice: Scan slice to use
        :param channel: Scan channel to use
        :returns: None
        """
        from .utils import caiman_interface as cmn

        # Get location matrix
        location_matrix = self.get_all_masks(slice, channel)

        # Get correlation image if defined
        image_rel = ExtractRaw.GalvoCorrelationImage() & self & {'slice': slice,
                                                                 'channel': channel}
        correlation_image = image_rel.fetch1['correlation_image'] if image_rel else None

        # Draw contours
        cmn.plot_contours(location_matrix, correlation_image)

    def plot_impulse_responses(self, slice=1, channel=1, num_timepoints=100):
        """ Plots the individual impulse response functions for all traces assuming an
        autoregressive process (p > 0).

        :param int num_timepoints: The number of points after impulse to use for plotting.

        :returns Figure. You can call show() on it.
        :rtype: matplotlib.figure.Figure
        """
        ar_rel = ExtractRaw.ARCoefficients() & self & {'slice': slice, 'channel': channel}
        fps = (Prepare.Galvo() & self).fetch1['fps']

        # Get AR coefficients
        ar_coefficients = ar_rel.fetch['g'] if ar_rel else None

        if ar_coefficients is not None:
            fig = plt.figure()
            x_axis = np.arange(num_timepoints) / fps # make it seconds

            # Over each trace
            for g in ar_coefficients:
                AR_order = len(g)

                # Calculate impulse response function
                irf = np.zeros(num_timepoints)
                irf[0] = 1  # initial spike
                for i in range(1, num_timepoints):
                    if i <= AR_order: # start of the array needs special care
                        irf[i] = np.sum(g[:i] * irf[i - 1:: -1])
                    else:
                        irf[i] = np.sum(g * irf[i - 1: i - AR_order - 1: -1])

                # Plot
                plt.plot(x_axis, irf)

            return fig

    def get_all_masks(self, slice, channel):
        """Returns an image_width x image_height x num_masks matrix with all masks."""
        mask_rel = ExtractRaw.GalvoROI() & self & {'slice': slice, 'channel': channel}

        # Get masks
        image_height, image_width = (Prepare.Galvo() & self).fetch1['px_height',
                                                                    'px_width']
        mask_pixels, mask_weights = mask_rel.fetch.order_by('trace_id')['mask_pixels',
                                                                        'mask_weights']

        # Reshape masks
        location_matrix = ExtractRaw.GalvoROI.reshape_masks(mask_pixels, mask_weights,
                                                            image_height, image_width)

        return location_matrix

    def get_all_traces(self, slice, channel):
        """ Returns a num_traces x num_timesteps matrix with all traces."""
        trace_rel = ExtractRaw.Trace() * ExtractRaw.GalvoROI() & self & {'slice': slice,
                                                                         'channel': channel}
        # Get traces
        raw_traces = trace_rel.fetch.order_by('trace_id')['raw_trace']

        # Reshape traces
        raw_traces = np.array([x.squeeze() for x in raw_traces])

        return raw_traces

@schema
class Sync(dj.Imported):
    definition = """
    -> Prepare
    ---
    -> vis.Session
    first_trial                 : int                           # first trial index from vis.Trial overlapping recording
    last_trial                  : int                           # last trial index from vis.Trial overlapping recording
    signal_start_time           : double                        # (s) signal start time on stimulus clock
    signal_duration             : double                        # (s) signal duration on stimulus time
    frame_times = null          : longblob                      # times of frames and slices
    sync_ts=CURRENT_TIMESTAMP   : timestamp                     # automatic
    """


@schema
class ComputeTraces(dj.Computed):
    definition = """   # compute traces
    -> ExtractRaw
    ---
    """

    class Trace(dj.Part):
        definition = """  # final calcium trace but before spike extraction or filtering
        -> ComputeTraces
        trace_id             : smallint                     #
        ---
        trace = null         : longblob                     # leave null same as ExtractRaw.Trace
        """

    @property
    def key_source(self):
        return (ExtractRaw() & ExtractRaw.Trace()).proj()

    @staticmethod
    def get_band_emission(fluorophore, center, band_width):
        from scipy import integrate as integr
        pass_band = (center - band_width / 2, center + band_width / 2)
        nu_loaded, s_loaded = (experiment.Fluorophore.EmissionSpectrum() &
                               dict(fluorophore=fluorophore, loaded=1)).fetch1['wavelength', 'fluorescence']

        nu_free, s_free = (experiment.Fluorophore.EmissionSpectrum() &
                           dict(fluorophore=fluorophore, loaded=0)).fetch1['wavelength', 'fluorescence']

        f_loaded = lambda xx: np.interp(xx, nu_loaded, s_loaded)
        f_free = lambda xx: np.interp(xx, nu_free, s_free)
        return integr.quad(f_free, *pass_band)[0], integr.quad(f_loaded, *pass_band)[0]

    @staticmethod
    def estimate_twitch_ratio(x, y, fps, df1, df2):
        from scipy import signal, stats

        # low pass filter for unsharp masking
        hh = signal.hamming(2 * np.round(fps / 0.03) + 1)
        hh /= hh.sum()

        # high pass filter for heavy denoising
        hl = signal.hamming(2 * np.round(fps / 8) + 1)
        hl /= hl.sum()
        x = mirrconv(x - mirrconv(x, hh), hl)
        y = mirrconv(y - mirrconv(y, hh), hl)

        slope, intercept, _, p, _ = stats.linregress(x, y)
        slope = -1 if slope >= 0 else slope
        return df2 / df1 / slope

    def _make_tuples(self, key):
        if ExtractRaw.Trace() & key:
            fluorophore = (experiment.Session.Fluorophore() & key).fetch1['fluorophore']
            if fluorophore != 'Twitch2B':
                print('Populating', key)

                def remove_channel(x):
                    x.pop('channel')
                    return x

                self.insert1(key)
                self.Trace().insert(
                    [remove_channel(x) for x in (ExtractRaw.Trace() & key).proj(trace='raw_trace').fetch.as_dict])
            elif fluorophore == 'Twitch2B':
                # --- get channel indices and filter passbands for twitch settings
                filters = experiment.PMTFilterSet() * experiment.PMTFilterSet.Channel() \
                          & dict(pmt_filter_set='2P3 blue-green A')
                fps = (Prepare.Galvo() & key).fetch1['fps']
                green_idx, green_center, green_pb = \
                    (filters & dict(color='green')).fetch1['pmt_channel', 'spectrum_center', 'spectrum_bandwidth']
                blue_idx, blue_center, blue_pb = \
                    (filters & dict(color='blue')).fetch1['pmt_channel', 'spectrum_center', 'spectrum_bandwidth']

                # --- compute theoretical emission over filter spectra
                g_free, g_loaded = self.get_band_emission(fluorophore, green_center, green_pb)
                b_free, b_loaded = self.get_band_emission(fluorophore, blue_center, blue_pb)
                dg = g_loaded - g_free
                db = b_loaded - b_free

                green = (ExtractRaw.Trace() & dict(key, channel=green_idx)).proj(green='channel',
                                                                                 green_trace='raw_trace')
                blue = (ExtractRaw.Trace() & dict(key, channel=blue_idx)).proj(blue='channel',
                                                                               blue_trace='raw_trace')

                self.insert1(key)
                for trace_id, gt, bt in zip(*(green * blue).fetch['trace_id', 'green_trace', 'blue_trace']):
                    print(
                        '\tProcessing animal_id: {animal_id}\t session: {session}\t scan_idx: {scan_idx}\ttrace: {trace_id}'.format(
                            trace_id=trace_id, **key))
                    gt, bt = gt.squeeze(), bt.squeeze()
                    start = notnan(gt * bt)
                    end = notnan(gt * bt, len(gt) - 1, increment=-1)
                    gamma = self.estimate_twitch_ratio(gt[start:end], bt[start:end], fps, dg, db)

                    x = np.zeros_like(gt) * np.NaN
                    gt, bt = gt[start:end], bt[start:end]
                    r = (gt - bt) / (gt + bt)
                    x[start:end] = (-b_free + g_free * gamma - r * (b_free + g_free * gamma)) / \
                                   (db - dg * gamma + r * (db + dg * gamma))

                    trace_key = dict(key, trace_id=trace_id, trace=x.astype(np.float32)[:, None])
                    self.Trace().insert1(trace_key)


@schema
class SpikeMethod(dj.Lookup):
    definition = """
    spike_method   :  smallint   # spike inference method
    ---
    spike_method_name     : varchar(16)   #  short name to identify the spike inference method
    spike_method_details  : varchar(255)  #  more details about
    language :  enum('matlab','python')   #  implementation language
    """

    contents = [
        [2, "oopsi", "nonnegative sparse deconvolution from Vogelstein (2010)", "python"],
        [3, "stm", "spike triggered mixture model from Theis et al. (2016)", "python"],
        [5, "nmf", "", "matlab"]
    ]

    def spike_traces(self, X, fps):
        try:
            import c2s
        except ImportError:
            warn("c2s was not found. You won't be able to populate ExtracSpikes")
        assert self.fetch1['language'] == 'python', "This tuple cannot be computed in python."
        if self.fetch1['spike_method'] == 3:
            N = len(X)
            for i, trace in enumerate(X):
                print('Predicting trace %i/%i' % (i + 1, N))
                tr0 = np.array(trace.pop('trace').squeeze())
                start = notnan(tr0)
                end = notnan(tr0, len(tr0) - 1, increment=-1)
                trace['calcium'] = np.atleast_2d(tr0[start:end + 1])

                trace['fps'] = fps
                data = c2s.preprocess([trace], fps=fps)
                data = c2s.predict(data, verbosity=0)

                tr0[start:end + 1] = data[0].pop('predictions')
                data[0]['rate_trace'] = tr0.T
                data[0].pop('calcium')
                data[0].pop('fps')

                yield data[0]


@schema
class Spikes(dj.Computed):
    definition = """  # infer spikes from calcium traces
    -> ComputeTraces
    -> SpikeMethod
    """

    @property
    def key_source(self):
        return (ComputeTraces() * SpikeMethod() & "language='python'").proj()

    class RateTrace(dj.Part):
        definition = """  # Inferred
        -> Spikes
        -> ExtractRaw
        trace_id  : smallint
        ---
        rate_trace = null  : longblob     # leave null same as ExtractRaw.Trace
        """

    def plot_traces(self, outdir='./'):

        gs = plt.GridSpec(2, 5)
        for key in (ComputeTraces.Trace() & self).fetch.keys():
            print('Processing', key)
            fps = (Prepare.Galvo() & key).fetch1['fps']

            hs = int(np.round(fps * 30))

            fig = plt.figure(figsize=(10, 4))
            ax_ca = fig.add_subplot(gs[0, :3])
            ax_sp = fig.add_subplot(gs[1, :3], sharex=ax_ca)

            ax_cas = fig.add_subplot(gs[0, 3:], sharey=ax_ca)
            ax_sps = fig.add_subplot(gs[1, 3:], sharex=ax_cas, sharey=ax_sp)

            ca = (ComputeTraces.Trace() & key).fetch1['trace'].squeeze()
            t = np.arange(len(ca)) / fps
            ax_ca.plot(t, ca, 'k')
            loc = None
            for sp, meth in zip(*(self.RateTrace() * SpikeMethod() & key).fetch['rate_trace', 'spike_method_name']):
                ax_sp.plot(t, sp, label=meth)
                # --- plot zoom in
                if loc is None:
                    n = len(sp)
                    tmp = np.array(sp)
                    tmp[np.isnan(tmp)] = 0
                    loc = np.argmax(np.convolve(tmp, np.ones(hs) / hs, mode='same'))
                    loc = max(loc - hs // 2, 0)
                    loc = n - hs if loc > n - hs else loc
                    ax_cas.plot(t[loc:loc + hs], ca[loc:loc + hs], 'k')
                    ax_ca.fill_between([t[loc], t[loc + hs - 1]], np.nanmin(ca) * np.ones(2),
                                       np.nanmax(ca) * np.ones(2),
                                       color='dodgerblue', zorder=-10)
                ax_sps.plot(t[loc:loc + hs], sp[loc:loc + hs], label=meth)

            ax_sp.set_xlabel('time [s]')
            ax_sps.set_xlabel('time [s]')

            ax_sp.legend()
            ax_sps.legend()

            try:
                sh.mkdir('-p', os.path.expanduser(outdir) + '/session{session}/scan_idx{scan_idx}/'.format(**key))
            except:
                pass

            fig.tight_layout()
            plt.savefig(outdir +
                        "/session{session}/scan_idx{scan_idx}/trace{trace_id:03d}_animal_id_{animal_id}.png".format(
                            **key))
            plt.close(fig)

    def _make_tuples(self, key):
        print('Populating Spikes for ', key, end='...', flush=True)
        method = (SpikeMethod() & key).fetch1['spike_method_name']
        if method == 'stm':
            prep = (Prepare() * Prepare.Aod() & key) or (Prepare() * Prepare.Galvo() & key)
            fps = prep.fetch1['fps']
            X = [dict(trace=fill_nans(x['trace'].astype('float64'))) for x in
                 (ComputeTraces.Trace() & key).proj('trace').fetch.as_dict]

            self.insert1(key)
            for x in (SpikeMethod() & key).spike_traces(X, fps):
                self.RateTrace().insert1(dict(key, **x))
        elif method == 'nmf':
            if ExtractRaw.SpikeRate() & key:
                self.insert1(key)
                for x in (ExtractRaw.SpikeRate() & key).fetch.as_dict:
                    x['rate_trace'] = x.pop('spike_trace')
                    x.pop('channel')
                    self.RateTrace().insert1(dict(key, **x))
        elif method == 'oopsi':
            prep = (Prepare() * Prepare.Aod() & key) or (Prepare() * Prepare.Galvo() & key)
            self.insert1(key)
            fps = prep.fetch1['fps']
            part = self.RateTrace()
            for trace, trace_key in zip(*(ComputeTraces.Trace() & key).fetch['trace', dj.key]):
                trace = pyfnnd.deconvolve(fill_nans(np.float64(trace.flatten())), dt=1 / fps)[0]
                part.insert1(dict(trace_key, rate_trace=trace.astype(np.float32)[:, np.newaxis], **key))
        else:
            raise NotImplementedError('Method {spike_method} not implemented.'.format(**key))
        print('Done', flush=True)


@schema
class EyeQuality(dj.Lookup):
    definition = """
    # Different eye quality definitions for Tracking

    eye_quality                : smallint
    ---
    description                : varchar(255)
    """

    contents = [
        (-1, 'unusable'),
        (0, 'good quality'),
        (1, 'poor quality'),
        (2, 'very poor quality (not well centered, pupil not fully visible)'),
        (3, 'good (but pupil is not the brightest spot)'),
        (4, 'very dark'),
        (5, 'like 4 but more slack in ratio'),
    ]


@schema
class BehaviorSync(dj.Imported):
    definition = """
    -> experiment.Scan
    ---
    frame_times                  : longblob # time stamp of imaging frame on behavior clock
    """


@schema
class Eye(dj.Imported):
    definition = """
    # eye velocity and timestamps

    -> experiment.Scan
    ---
    -> EyeQuality
    eye_roi                     : tinyblob  # manual roi containing eye in full-size movie
    eye_time                    : longblob  # timestamps of each frame in seconds, with same t=0 as patch and ball data
    total_frames                : int       # total number of frames in movie.
    eye_ts=CURRENT_TIMESTAMP    : timestamp # automatic
    """

    def unpopulated(self):
        """
        Returns all keys from Scan()*Session() that are not in Eye but have a video.


        :param path_prefix: prefix to the path to find the video (usually '/mnt/', but empty by default)
        """

        rel = experiment.Session() * experiment.Scan.EyeVideo()
        path_prefix = config['path.mounts']
        restr = [k for k in (rel - self).proj('behavior_path', 'filename').fetch.as_dict() if
                 os.path.exists("{path_prefix}/{behavior_path}/{filename}".format(path_prefix=path_prefix, **k))]
        return (rel - self) & restr

    def grab_timestamps_and_frames(self, key, n_sample_frames=10):

        import cv2

        rel = experiment.Session() * experiment.Scan.EyeVideo() * experiment.Scan.BehaviorFile().proj(
            hdf_file='filename')

        info = (rel & key).fetch1()

        avi_path = lab.Paths().get_local_path("{behavior_path}/{filename}".format(**info))
        # replace number by %d for hdf-file reader

        tmp = info['hdf_file'].split('.')
        if not '%d' in tmp[0]:
            info['hdf_file'] = tmp[0][:-1] + '%d.' + tmp[-1]

        hdf_path = lab.Paths().get_local_path("{behavior_path}/{hdf_file}".format(**info))

        data = read_video_hdf5(hdf_path)
        packet_length = data['analogPacketLen']
        dat_time, _ = ts2sec(data['ts'], packet_length)

        if float(data['version']) == 2.:
            cam_key = 'eyecam_ts'
            eye_time, _ = ts2sec(data[cam_key][0])
        else:
            cam_key = 'cam1ts' if info['rig'] == '2P3' else  'cam2ts'
            eye_time, _ = ts2sec(data[cam_key])

        total_frames = len(eye_time)

        frame_idx = np.floor(np.linspace(0, total_frames - 1, n_sample_frames))

        cap = cv2.VideoCapture(avi_path)
        no_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        if total_frames != no_frames:
            warn("{total_frames} timestamps, but {no_frames}  movie frames.".format(total_frames=total_frames,
                                                                                    no_frames=no_frames))
            if total_frames > no_frames and total_frames and no_frames:
                total_frames = no_frames
                eye_time = eye_time[:total_frames]
                frame_idx = np.round(np.linspace(0, total_frames - 1, n_sample_frames)).astype(int)
            else:
                raise PipelineException('Can not reconcile frame count', key)
        frames = []
        for frame_pos in frame_idx:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_pos)
            ret, frame = cap.read()

            frames.append(np.asarray(frame, dtype=float)[..., 0])
        frames = np.stack(frames, axis=2)
        return eye_time, frames, total_frames

    def _make_tuples(self, key):
        key['eye_time'], frames, key['total_frames'] = self.grab_timestamps_and_frames(key)

        try:
            import cv2
            print('Drag window and print q when done')
            rg = CVROIGrabber(frames.mean(axis=2))
            rg.grab()
        except ImportError:
            rg = ROIGrabber(frames.mean(axis=2))

        with dj.config(display__width=50):
            print(EyeQuality())
        key['eye_quality'] = int(input("Enter the quality of the eye: "))
        key['eye_roi'] = rg.roi
        self.insert1(key)
        print('[Done]')
        if input('Do you want to stop? y/N: ') == 'y':
            self.connection.commit_transaction()
            raise PipelineException('User interrupted population.')



schema.spawn_missing_classes()
