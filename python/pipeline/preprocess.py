import datajoint as dj
from . import experiment, psy
from warnings import warn
try:
    import c2s
except:
    warn("c2s was not found. You won't be able to populate ExtracSpikes")

from distutils.version import StrictVersion
assert StrictVersion(dj.__version__) >= StrictVersion('0.2.8')

schema = dj.schema('pipeline_preprocess', locals())


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
    definition = """  # pre-processing of a twp-photon scan
    -> Prepare
    -> Method
    """

    @property
    def key_source(self):
        return Prepare()*Method().proj() & \
               dj.OrList(Prepare.Aod()*Method.Aod(), Prepare.Galvo()*Method.Galvo())

    class Trace(dj.Part):
        definition = """  # raw trace, common to Galvo
        -> ExtractRaw
        -> Channel
        trace_id  : smallint
        ---
        raw_trace : longblob     # unprocessed calcium trace
        """

    class GalvoSegmentation(dj.Part):
        definition = """  # segmentation of galvo movies
        -> ExtractRaw
        -> Slice
        ---
        segmentation_mask=null  :  longblob
        """

    class GalvoROI(dj.Part):
        definition = """  # region of interest produced by segmentation
        -> ExtractRaw.GalvoSegmentation
        -> ExtractRaw.Trace
        ---
        mask_pixels          :longblob      # indices into the image in column major (Fortran) order
        mask_weights = null  :longblob      # weights of the mask at the indices above
        """

    class SpikeRate(dj.Part):
        definition = """
        # spike trace extracted while segmentation
        -> ExtractRaw.Trace
        ---
        spike_trace :longblob
        """


    def _make_tuples(self, key):
        """ implemented in matlab """
        raise NotImplementedError


@schema
class Sync(dj.Imported):
    definition = """
    -> Prepare
    ---
    -> psy.Session
    first_trial                 : int                           # first trial index from psy.Trial overlapping recording
    last_trial                  : int                           # last trial index from psy.Trial overlapping recording
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
        -> ExtractRaw.Trace
        ---
        trace = null  : longblob     # leave null same as ExtractRaw.Trace
        """

    def _make_tuples(self, key):
        if (experiment.Session.Fluorophore()& key).fetch1['fluorophore'] != 'Twitch2B' and (ExtractRaw.Trace() & key):
            print('Populating', key)
            self.insert1(key)
            self.Trace().insert((ExtractRaw.Trace() & key).proj(trace='raw_trace').fetch())




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
        [2, "fastoopsi", "nonnegative sparse deconvolution from Vogelstein (2010)", "matlab"],
        [3, "stm", "spike triggered mixture model from Theis et al. (2016)",  "python"],
        [4, "improved oopsi", "", "matlab"]
    ]


    def infer_spikes(self, X, dt, trace_name='trace'):
        assert self.fetch1['language'] == 'python', "This tuple cannot be computed in python."
        fps = 1 / dt
        spike_rates = []
        N = len(X)
        for i, trace in enumerate(X):
            print('Predicting trace %i/%i' % (i + 1, N))
            trace['calcium'] = trace.pop(trace_name).T
            trace['fps'] = fps

            data = c2s.preprocess([trace], fps=fps)
            data = c2s.predict(data, verbosity=0)
            data[0]['spike_trace'] = data[0].pop('predictions').T
            data[0].pop('calcium')
            data[0].pop('fps')
            spike_rates.append(data[0])
        return spike_rates

@schema
class Spikes(dj.Computed):
    definition = """  # infer spikes from calcium traces
    -> ComputeTraces
    -> SpikeMethod
    """

    class RateTrace(dj.Part):
        definition = """  # Inferred
        -> Spikes
        -> ComputeTraces.Trace
        ---
        rate_trace = null  : longblob     # leave null same as ExtractRaw.Trace
        """

    def _make_tuples(self, key):
        dt = 1 / (Prepare() & key).fetch1['fps']
        X = (ComputeTraces() & key).project('trace').fetch.as_dict()
        X = (SpikeMethod() & key).infer_spikes(X, dt)
        for x in X:
            self.insert1(dict(key, **x))

schema.spawn_missing_classes()

