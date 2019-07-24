# Skip to content
 
# Search or jump to…

# Pull requests
# Issues
# Marketplace
# Explore
 
# @SmartIntello 
# 5
# 0 1 NSLS-II-SRX/profile_collection
#  Code  Issues 0  Pull requests 0  Projects 0  Wiki  Security  Insights
# profile_collection/startup/91-flyscans.py
#  Andy Kiss Updates for running dexela
# 9e99604 23 days ago
# @tacaswell @mrakitin @jrmlhermitte @danielballan
# 1044 lines (886 sloc)  40.6 KB
    
# print(f'Loading {__file__}')

#####
# Pseudocode for fly scanning
# User supplies:
# X start       Xstart is used for gating, Xo is an X stage value where the scan
#   will start
# Y start       Ystart is used for gating, Yo is an Y stage value where the scan
#   will start
# range X       scan X size
# dwell         time spent per atom, range X and dwell give stage speed
# range Y       scan Y size
#####

#Transverse flyer for SRX: fly X, step Y
#Aerotech X stage, DC motor need to set desired velocity
#Zebra staged, data read and written to file, file registered
#Options:  open and write to Xspress3 HDF5, write to HDF5 and link in
#   Xspress3 data, write separately in some format and register later
#
import numpy as np
from bluesky.plans import (scan, )
from bluesky.plan_stubs import (one_1d_step, kickoff, collect, complete,
                                abs_set, mv)

import bluesky.plan_stubs as bps


from bluesky.preprocessors import (stage_decorator,
                                   run_decorator, subs_decorator,
                                   monitor_during_decorator)
from ophyd.sim import NullStatus
from bluesky.callbacks import CallbackBase, LiveGrid
from ophyd import Device
import uuid
#import h5py
from collections import ChainMap
from ophyd.areadetector.filestore_mixins import resource_factory

#from hxntools.handlers import register
# register(db)


class SRXFlyer1Axis(Device):
    # LARGE_FILE_DIRECTORY_WRITE_PATH = '/nsls2/xf05id1/XF05ID1/data/2018-1/fly_scan_ancillary/'
#    LARGE_FILE_DIRECTORY_READ_PATH = '/tmp/test_data'
#    LARGE_FILE_DIRECTORY_WRITE_PATH = '/tmp/fly_scan_ancillary'
    # LARGE_FILE_DIRECTORY_READ_PATH = '/nsls2/xf05id1/XF05ID1/data/2018-1/fly_scan_ancillary/'
    #"This is the Zebra."


    xstart = 29.621
    xstop = 30.121  
    xnum = 101
    ystart = 19.707
    ystop = 20.207
    ynum = 101
    dwell = 0.25


    KNOWN_DETS = {'xs', 'xs2', 'merlin', 'dexela'}
    @property
    def encoder(self):
        return self._encoder

    @property
    def detectors(self):
        return tuple(self._dets)

    @detectors.setter
    def detectors(self, value):
        dets = tuple(value)
        if not all(d.name in self.KNOWN_DETS
                   for d in dets):
            raise ValueError(f'One or more of {[d.name for d in dets]}'
                             f'is not known to the zebara.  '
                             f'The known detectors are {self.KNOWN_DETS})')
        self._dets = dets

    @property
    def sclr(self):
        return self._sis

    def __init__(self, encoder, dets, sclr1, fast_axis, *, reg=db.reg, **kwargs):
        super().__init__('', parent=None, **kwargs)
        self._mode = 'idle'
        self._encoder = encoder
        self._dets = dets
        self._sis = sclr1
        self._filestore_resource = None

        self._fast_axis = fast_axis

        if self._fast_axis == 'HOR':
            self.stage_sigs[self._encoder.pc.enc] = 'Enc2'
            self.stage_sigs[self._encoder.pc.dir] = 'Positive'
            self.stage_sigs[self._encoder.pc.enc_res2] = 5E-6
        elif self._fast_axis == 'VER':
            self.stage_sigs[self._encoder.pc.enc] = 'Enc1'
            self.stage_sigs[self._encoder.pc.dir] = 'Positive'
            self.stage_sigs[self._encoder.pc.enc_res1] = 5E-6
        elif self._fast_axis == 'DET2HOR':
            self.stage_sigs[self._encoder.pc.enc] = 'Enc3'
            self.stage_sigs[self._encoder.pc.dir] = 'Positive'
            self.stage_sigs[self._encoder.pc.enc_res1] = 5E-5
        elif self._fast_axis == 'DET2VER':
            self.stage_sigs[self._encoder.pc.enc] = 'Enc4'
            self.stage_sigs[self._encoder.pc.dir] = 'Positive'
            self.stage_sigs[self._encoder.pc.enc_res1] = 5E-5

        # gating info for encoder capture
        self.stage_sigs[self._encoder.pc.gate_num] = 1
        self.stage_sigs[self._encoder.pc.pulse_start] = 0

        #pc gate output is 31 for zebra.  use it to trigger xspress3 and I0
        self.stage_sigs[self._encoder.output1.ttl.addr] = 31
        self.stage_sigs[self._encoder.output3.ttl.addr] = 31
        # this is for the merlin
        self.stage_sigs[self._encoder.output2.ttl.addr] = 53
        # this is for the dexela
        self.stage_sigs[self._encoder.output4.ttl.addr] = 55
        # this is for the xs2 
        # self.stage_sigs[self._encoder.output4.ttl.addr] = 31

        self.stage_sigs[self._encoder.pc.enc_pos1_sync] = 1
        self.stage_sigs[self._encoder.pc.enc_pos2_sync] = 1
        self.stage_sigs[self._encoder.pc.enc_pos3_sync] = 1
        self.stage_sigs[self._encoder.pc.enc_pos4_sync] = 1

        #put SIS3820 into single count (not autocount) mode
        self.stage_sigs[self._sis.count_mode] = 0

        #stop the SIS3820
        self._sis.stop_all.put(1)

        self._encoder.pc.block_state_reset.put(1)
        self.reg = reg
        self._document_cache = []
        self._last_bulk = None

    def stage(self):
        super().stage()

    def describe_collect(self):

        ext_spec = 'FileStore:'

        spec = {'external': ext_spec,
            'dtype' : 'array',
            'shape' : [self._npts],
            'source': ''  # make this the PV of the array the det is writing
        }

        desc = OrderedDict()
        for chan in ('time', 'enc1'):
            desc[chan] = spec
            desc[chan]['source'] = getattr(self._encoder.pc.data, chan).pvname

        # handle the detectors we are going to get
        for d in self._dets:
            desc.update(d.describe())

        # handle the ion chamber that the zebra is collecting
        desc['i0'] = spec
        desc['i0']['source'] = self._sis.mca2.pvname
        desc['i0_time'] = spec
        desc['i0_time']['source'] = self._sis.mca1.pvname
        desc['im'] = spec
        desc['im']['source'] = self._sis.mca3.pvname
        desc['it'] = spec
        desc['it']['source'] = self._sis.mca4.pvname

        return {'stream0': desc}


    def kickoff(self, *, xstart, xstop, xnum, dwell):
        self._encoder.pc.arm.put(0)
        self._mode = 'kicked off'
        self._npts = int(xnum)
        extent = xstop - xstart
        pxsize = extent / (xnum-1)
        #1 ms delay between pulses
        decrement = ((pxsize / dwell) * 0.002)
        if decrement < 4e-6:
            # print('Changing the pulse width')
            decrement = 4e-6
        self._encoder.pc.gate_start.put(xstart)
        #self._encoder.pc.gate_step.put(extent+0.01)
        #self._encoder.pc.gate_width.put(extent+0.005)
        self._encoder.pc.gate_step.put(extent+0.0005)
        self._encoder.pc.gate_width.put(extent+0.0001)
        self._encoder.pc.pulse_max.put(xnum)
#        self._encoder.pc.pulse_step.put(dwell)
#        self._encoder.pc.pulse_width.put(dwell - 0.005)
#        self._encoder.pc.pulse_step.put(extent/xnum)
#        self._encoder.pc.pulse_width.put(extent/xnum-decrement)
        self._encoder.pc.pulse_step.put(pxsize)
        self._encoder.pc.pulse_width.put(pxsize-decrement)
        # If decrement is too small, then zebra will not send individual pulses
        # but integrate over the entire line
        # if (self._encoder.pc.pulse_step.get() == self._encoder.pc.pulse_width.get()):

        self._encoder.pc.pulse_start.put(0.0)
        #self._encoder.pc.pulse_step.put(dwell)
        #self._encoder.pc.pulse_width.put(dwell-0.001)

        # If both values are not synced, then the X-position was not updating
        # during the scan and will remain at the initial value
        # - AMK
        self._encoder.pc.enc_pos1_sync.put(1, wait=True)  # Sample Y
        self._encoder.pc.enc_pos2_sync.put(1, wait=True)  # Sample X
        self._encoder.pc.enc_pos3_sync.put(1, wait=True)  # Det2 Stage X
        self._encoder.pc.enc_pos4_sync.put(1, wait=True)  # Det2 Stage Y
        # yield from abs_set(self._encoder.pc.enc_pos1_sync, 1, wait=True)
        # yield from abs_set(self._encoder.pc.enc_pos2_sync, 1, wait=True)

        self._encoder.pc.arm.put(1)
        # yield from abs_set(self._encoder.pc.arm, 1, wait=True)

        ## THIS MUST CHANGE!!!!
        # if self._fast_axis == 'VER':
        #     self._encoder.pc.enc_pos1_sync.put(1)
        # elif self._fast_axis == 'HOR':
        #     self._encoder.pc.enc_pos2_sync.put(1)
        # If both values are not synced, then the X-position was not updating
        # during the scan and will remain at the initial value
        #
        # Moved to before arming
        # - AMK
        # self._encoder.pc.enc_pos1_sync.put(1)
        # self._encoder.pc.enc_pos2_sync.put(1)

        st = NullStatus()  # TODO Return a status object *first* and do the above asynchronously.
        return st

    # def complete(self):
    #     """
    #     Call this when all needed data has been collected. This has no idea
    #     whether that is true, so it will obligingly stop immediately. It is
    #     up to the caller to ensure that the motion is actually complete.
    #     """
    #     # Our acquisition complete PV is : XF:05IDD-ES:1{Dev:Zebra1}:ARRAY_ACQ
    #     while self._encoder.pc.data_in_progress.get() == 1:
    #         ttime.sleep(.1)
    #         #poll()
    #     ttime.sleep(.1)
    #     self._mode = 'complete'
    #     # self._encoder.pc.arm.put(0) # sanity check; this should
    #     # happen automatically this does the same as the above, but
    #     # also aborts data collection
    #     self._encoder.pc.block_state_reset.put(1)
    #     #see triggering errors of the xspress3 on suspension.  This is
    #     #to test the reset of the xspress3 after a line.


    #     for d in self._dets:
    #         # xs.settings.acquire.put(0)
    #         d.stop(success=True)

    #     self.__filename = '{}.h5'.format(uuid.uuid4())
    #     self.__filename_sis = '{}.h5'.format(uuid.uuid4())
    #     self.__read_filepath = os.path.join(self.LARGE_FILE_DIRECTORY_READ_PATH,
    #                                         self.__filename)
    #     self.__read_filepath_sis = os.path.join(self.LARGE_FILE_DIRECTORY_READ_PATH,
    #                                             self.__filename_sis)
    #     self.__write_filepath = os.path.join(self.LARGE_FILE_DIRECTORY_WRITE_PATH,
    #                                          self.__filename)
    #     self.__write_filepath_sis = os.path.join(self.LARGE_FILE_DIRECTORY_WRITE_PATH,
    #                                              self.__filename_sis)

    #     self.__filestore_resource, datum_factory_z = resource_factory(
    #         'ZEBRA_HDF51', root='/',
    #         resource_path=self.__read_filepath,
    #         resource_kwargs={}, path_semantics='posix')
    #     self.__filestore_resource_sis, datum_factory_sis = resource_factory(
    #         'SIS_HDF51', root='/',
    #         resource_path=self.__read_filepath_sis,
    #         resource_kwargs={},
    #         path_semantics='posix')

    #     time_datum = datum_factory_z({'column': 'time'})
    #     enc1_datum = datum_factory_z({'column': 'enc1'})
    #     sis_datum =  datum_factory_sis({'column': 'i0'})
    #     sis_datum_im =  datum_factory_sis({'column': 'im'})
    #     sis_datum_it =  datum_factory_sis({'column': 'it'})
    #     sis_time =  datum_factory_sis({'column': 'time'})

    #     self._document_cache.extend(('resource', d) for d in (self.__filestore_resource,
    #                                                          self.__filestore_resource_sis))
    #     self._document_cache.extend(('datum', d) for d in (time_datum, enc1_datum,
    #                                                       sis_datum, sis_time, sis_datum_im, sis_datum_it))

    #     # grab the asset documents from all of the child detectors
    #     for d in self._dets:
    #         self._document_cache.extend(d.collect_asset_docs())

    #     # Write the file.
    #     export_zebra_data(self._encoder, self.__write_filepath,self._fast_axis)
    #     export_sis_data(self._sis, self.__write_filepath_sis)

    #     # Yield a (partial) Event document. The RunEngine will put this
    #     # into metadatastore, as it does all readings.
    #     self._last_bulk =  {
    #         'time': time.time(), 'seq_num': 1,
    #         'data': {'time': time_datum['datum_id'],
    #                  'enc1': enc1_datum['datum_id'],
    #                  'i0': sis_datum['datum_id'],
    #                  'i0_time': sis_time['datum_id'],
    #                  'im': sis_datum_im['datum_id'],
    #                  'it': sis_datum_it['datum_id']},
    #         'timestamps': {'time': time_datum['datum_id'],  # not a typo#
    #                        'enc1': time_datum['datum_id'],
    #                        'i0': sis_time['datum_id'],
    #                        'i0_time': sis_time['datum_id'],
    #                        'im': sis_datum_im['datum_id'],
    #                        'it': sis_datum_it['datum_id']}
    #     }
    #     for d in self._dets:
    #         reading = d.read()
    #         self._last_bulk['data'].update({k: v['value']
    #                                         for k, v in reading.items()})
    #         self._last_bulk['timestamps'].update({k: v['timestamp']
    #                                               for k, v in reading.items()})

    #     return NullStatus()

    def collect(self):
        # Create records in the FileStore database.
        # move this to stage because I thinkt hat describe_collect needs the
        # resource id
        # TODO use ophyd.areadectector.filestoer_mixins.resllource_factory here
        if self._last_bulk is None:
            raise Exception("the order of complete and collect is brittle and out "
                            "of sync. This device relies on in-order and 1:1 calls "
                            "between complete and collect to correctly create and stash "
                            "the asset registry documents")
        yield self._last_bulk
        self._last_bulk = None
        self._mode = 'idle'

    def collect_asset_docs(self):
        yield from iter(list(self._document_cache))
        self._document_cache.clear()

    def stop(self):
        self._encoder.pc.block_state_reset.put(1)
        pass

    def pause(self):
        "Pausing in the middle of a kickoff nukes the partial dataset."
        #self._encoder.pc.arm.put(0)
        self._encoder.pc.block_state_reset.put(1)
        self._sis.stop_all.put(1)
        for d in self._dets:
            if hasattr(d, 'settings'):
                d.settings.acquire.put(0)
            if hasattr(d, 'cam'):
                d.cam.acquire.put(0)
        self._mode = 'idle'
        self.unstage()

    def resume(self):
        self.stage()


# flying_zebra = SRXFlyer1Axis(zebra, [xs], sclr1, 'HOR', name='flying_zebra')
# flying_zebra_y = SRXFlyer1Axis(zebra, [xs], sclr1, 'VER', name='flying_zebra')
# NOTE: as of 2019-01-11, xs2 device is not available, as it's only used for
# specialized experiments.
# For confocal
# flying_zebra_x_xs2 = SRXFlyer1Axis(zebra, [xs2], sclr1, 'HOR', name='flying_zebra')
# flying_zebra_y_xs2 = SRXFlyer1Axis(zebra, [xs2], sclr1, 'VER', name='flying_zebra')
# For chip imaging
# flying_zebra_x_xs2 = SRXFlyer1Axis(zebra, xs2, sclr1, 'DET2HOR', name='flying_zebra')
# flying_zebra_y_xs2 = SRXFlyer1Axis(zebra, xs2, sclr1, 'DET2VER', name='flying_zebra')
# flying_zebra = SRXFlyer1Axis(zebra)


def export_zebra_data(zebra, filepath, fast_axis):
    j = 0
    while zebra.pc.data_in_progress.get()==1:
        print('waiting zebra')
        ttime.sleep(0.1)
        j += 1
        if j > 10:
            print('THE ZEBRA IS BEHAVING BADLY CARRYING ON')
            break

    #ttime.sleep(.5)
    time_d = zebra.pc.data.time.get()
    if fast_axis == 'HOR':
        enc1_d = zebra.pc.data.enc2.get()
    elif fast_axis == 'DET2HOR':
        enc1_d = zebra.pc.data.enc3.get()
    elif fast_axis == 'DET2VER':
        enc1_d = zebra.pc.data.enc4.get()
    else:
        enc1_d = zebra.pc.data.enc1.get()

    while len(time_d) == 0 or len(time_d) != len(enc1_d):
        time_d = zebra.pc.data.time.get()
        #enc1_d = zebra.pc.data.enc2.get()
        if fast_axis == 'HOR':
            enc1_d = zebra.pc.data.enc2.get()
        else:
            enc1_d = zebra.pc.data.enc1.get()

    size = (len(time_d),)
    with h5py.File(filepath, 'w') as f:
        dset0 = f.create_dataset("time",size,dtype='f')
        dset0[...] = np.array(time_d)
        dset1 = f.create_dataset("enc1",size,dtype='f')
        dset1[...] = np.array(enc1_d)
        f.close()

def export_sis_data(ion, filepath):
    t = ion.mca1.get(timeout=5.)
    i = ion.mca2.get(timeout=5.)
    im= ion.mca3.get(timeout=5.)
    it= ion.mca4.get(timeout=5.)
    while len(t) == 0 and len(t) != len(i):
        t = ion.mca1.get(timeout=5.)
        i = ion.mca2.get(timeout=5.)
        im= ion.mca3.get(timeout=5.)
        it= ion.mca4.get(timeout=5.)

    correct_length = zebra.pc.data.num_down.get()
    size = (len(t),)
    size2 = (len(i),)
    size3 = (len(im),)
    size4 = (len(it),)
    with h5py.File(filepath, 'w') as f:
        if len(t) != correct_length:
            correction_factor = (correct_length-len(t))
            new_t = [k for k in t] + [ 1e10 for _ in range(0,int(correction_factor)) ]
            new_i = [k for k in i] + [ 1e10 for _ in range(0,int(correction_factor)) ]
            new_im= [k for k in im] + [ 1e10 for _ in range(0,int(correction_factor)) ]
            new_it= [k for k in it] + [ 1e10 for _ in range(0,int(correction_factor)) ]
        else:
            correction_factor = 0
            new_t = t
            new_i = i
            new_im= im
            new_it= it
        dset0 = f.create_dataset("time",(correct_length,),dtype='f')
        dset0[...] = np.array(new_t)
        dset1 = f.create_dataset("i0",(correct_length,),dtype='f')
        dset1[...] = np.array(new_i)
        dset2 = f.create_dataset("im",(correct_length,),dtype='f')
        dset2[...] = np.array(new_im)
        dset3 = f.create_dataset("it",(correct_length,),dtype='f')
        dset3[...] = np.array(new_it)
        f.close()

# class ZebraHDF5Handler(HandlerBase):
#     # HANDLER_NAME = 'ZEBRA_HDF51'
#     def __init__(self, resource_fn):
#         self._handle = h5py.File(resource_fn, 'r')

#     def __call__(self, *, column):
#         return self._handle[column][:]

# class SISHDF5Handler(HandlerBase):
#     HANDLER_NAME = 'SIS_HDF51'
#     def __init__(self, resource_fn):
#         self._handle = h5py.File(resource_fn, 'r')

#     def __call__(self, *, column):
#         return self._handle[column][:]


# db.reg.register_handler('SIS_HDF51', SISHDF5Handler, overwrite=True)
# db.reg.register_handler('ZEBRA_HDF51', ZebraHDF5Handler, overwrite=True)


# class LiveZebraPlot(CallbackBase):

#     """
#     This is a really dumb approach but it gets the job done. To fix later.
#     """

#     def __init__(self, ax=None):
#         self._uid = None
#         self._desc_uid = None
#         if ax is None:
#             fig, ax = plt.subplots()
#         self.ax = ax
#         self.legend_title = 'sequence #'

#     def start(self, doc):
#         self._uid = doc['uid']

#     def descriptor(self, doc):
#         if doc['name'] == 'stream0':
#             self._desc_uid = doc['uid']

#     def bulk_events(self, docs):
#         # Don't actually use the docs, but use the fact that they have been
#         # emitted as a signal to go grab the data from the databroker now.
#         event_uids = [doc['uid'] for doc in docs[self._desc_uid]]
#         events = db.get_events(db[self._uid], stream_name='stream0', fields=['enc1', 'time'], fill=True)
#         for event in events:
#             if event['uid'] in event_uids:
#                 self.ax.plot(event['data']['time'], event['data']['enc1'], label=event['seq_num'])
#         self.ax.legend(loc=0, title=self.legend_title)

#     def stop(self, doc):
#         self._uid = None
#         self._desc_uid = None


# changed the flyer device to be aware of fast vs slow axis in a 2D scan
# should abstract this method to use fast and slow axes, rather than x and y
def scan_and_fly_base(detectors, xstart, xstop, xnum, ystart, ystop, ynum, dwell, *,

                      flying_zebra, xmotor, ymotor,

                      delta=None, shutter=True, align=False,

                      md=None):
    """Read IO from SIS3820.
    Zebra buffers x(t) points as a flyer.
    Xpress3 is our detector.
    The aerotech has the x and y positioners.
    delta should be chosen so that it takes about 0.5 sec to reach the gate??
    ymotor  slow axis
    xmotor  fast axis
    Parameters
    ----------
    Detectors : List[Device]
       These detectors must be known to the zebra
    xstart, xstop : float
    xnum : int
    ystart, ystop : float
    ynum : int
    dwell : float
       Dwell time in seconds
    flying_zebra : SRXFlyer1Axis
    xmotor, ymotor : EpicsMotor, kwarg only
        These should be known to the zebra
        # TODO sort out how to check this
    delta : float, optional, kwarg only
       offset on the ystage start position.  If not given, derive from
       dwell + pixel size
    align : bool, optional, kwarg only
       If True, try to align the beamline
    shutter : bool, optional, kwarg only
       If True, try to open the shutter
    """
    c2pitch_kill = EpicsSignal("XF:05IDA-OP:1{Mono:HDCM-Ax:P2}Cmd:Kill-Cmd")
    if md is None:
        md = {}

    # assign detectors to flying_zebra, this may fail
    flying_zebra.detectors = detectors
    # Setup detectors, combine the zebra, sclr, and the just set detector list
    detectors = (flying_zebra.encoder, flying_zebra.sclr) + flying_zebra.detectors

    dets_by_name = {d.name : d
                    for d in detectors}

    # set up the merlin
    if 'merlin' in dets_by_name:
        dpc = dets_by_name['merlin']
        # TODO use stage sigs
        # Set trigger mode
        # dpc.cam.trigger_mode.put(2)
        # Make sure we respect whatever the exposure time is set to
        if (dwell < 0.0066392):
            print('The Merlin should not operate faster than 7 ms.')
            print('Changing the scan dwell time to 7 ms.')
            dwell = 0.007
        # According to Ken's comments in hxntools, this is a de-bounce time
        # when in external trigger mode
        dpc.cam.stage_sigs['acquire_time'] = .25 * dwell - 0.0016392
        dpc.cam.stage_sigs['acquire_period'] = .5 * dwell
        dpc.cam.stage_sigs['num_images'] = 1
        dpc.stage_sigs['total_points'] = xnum
        dpc.hdf5.stage_sigs['num_capture'] = xnum
        del dpc

    # setup dexela
    if ('dexela' in dets_by_name):
        xrd = dets_by_name['dexela']
        xrd.cam.stage_sigs['acquire_time'] = 1.00 * dwell - 0.050
        xrd.cam.stage_sigs['acquire_period'] = 1.00 * dwell - 0.020
        del xrd

    # If delta is None, set delta based on time for acceleration
    if delta is None:
        # delta = 0.002  # old default value
        v = ((xstop - xstart) / (xnum-1)) / dwell  # compute "stage speed"
        t_acc = 1.0  # acceleration time, default 1.0 s
        delta = t_acc * v  # distance the stage will travel in t_acc

    # TODO can we do this move in parallel?
    yield from abs_set(ymotor, ystart, wait=True) # ready to move
    yield from abs_set(xmotor, xstart - delta, wait=True) # ready to move

    if shutter:
        yield from mv(shut_b, 'Open')

    if align:
        fly_ps = PeakStats(dcm.c2_pitch.name, i0.name)
        align_scan = bp.subs_wrapper(
            scan([flying_zebra.sclr], dcm.c2_pitch, -19.320, -19.360, 41),
            fly_ps)
        ret = yield from align_scan
        if ret is not None:
            yield from abs_set(dcm.c2_pitch, fly_ps.max[0], wait=True)
        #ttime.sleep(10)
        #yield from abs_set(c2pitch_kill, 1)

    md = ChainMap(md, {
        'plan_name': 'scan_and_fly',
        'detectors': [d.name for d in detectors],
        'dwell': dwell,
        'shape': (xnum, ynum),
        # 'scaninfo': {'type': 'XRF_fly',
        #              'raster': False,
        #              'fast_axis': flying_zebra._fast_axis},
        #              'theta': hf_stage.th.position,
        'scaninfo': {'type': 'E_tomo',
                     'raster': False,
                     'fast_axis': flying_zebra._fast_axis},
        'scan_params': [xstart, xstop, xnum, ystart, ystop, ynum, dwell],
        'scan_input': [xstart, xstop, xnum, ystart, ystop, ynum, dwell],
        'delta': delta
        }
    )

    # if (xs == 'xs2'):
    #     md['scaninfo']['type'] = 'XRF_E_tomo_fly'
    if ('xs2' in dets_by_name):
        md['scaninfo']['type'] = 'XRF_E_tomo_fly'

    @stage_decorator(flying_zebra.detectors)
    def fly_each_step(motor, step):
        "See http://nsls-ii.github.io/bluesky/plans.html#the-per-step-hook"
        # First, let 'scan' handle the normal y step, including a checkpoint.
        yield from one_1d_step([], motor, step)
        # yield from bps.sleep(1.0)  # wait for the "x motor" to move

        # Now do the x steps.
        v = ((xstop - xstart) / (xnum-1)) / dwell  # compute "stage speed"
        yield from abs_set(xmotor, xstart - delta, wait=True) # ready to move

        # TODO  Why are we re-trying the move?  This should be fixed at
        # a lower level
        # yield from bps.sleep(1.0)  # wait for the "x motor" to move
        x_set = xstart - delta
        x_dial = xmotor.user_readback.get()
        i = 0
        while (np.abs(x_set - x_dial) > 0.0001):
            if (i == 0):
                print('Waiting for motor to reach starting position...', end='')
            i = i + 1
            yield from abs_set(xmotor, xstart - delta, wait=True)
            yield from bps.sleep(1.0)
            x_dial = xmotor.user_readback.get()
        if (i != 0):
            print('done')

        yield from abs_set(xmotor.velocity, v, wait=True)  # set the "stage speed"

        # set up all of the detectors
        # TODO we should be able to move this out of the per-line call?!
        if 'xs' in dets_by_name:
            xs = dets_by_name['xs']
            yield from abs_set(xs.hdf5.num_capture, xnum, wait=True)
            yield from abs_set(xs.settings.num_images, xnum, wait=True)

        if 'xs2' in dets_by_name:
            xs2 = dets_by_name['xs2']
            yield from abs_set(xs2.hdf5.num_capture, xnum, wait=True)
            yield from abs_set(xs2.settings.num_images, xnum, wait=True)

        if 'merlin' in dets_by_name:
            merlin = dets_by_name['merlin']
            yield from abs_set(merlin.hdf5.num_capture, xnum, wait=True)
            yield from abs_set(merlin.cam.num_images, xnum, wait=True)

        if 'dexela' in dets_by_name:
            dexela = dets_by_name['dexela']
            yield from abs_set(dexela.hdf5.num_capture, xnum, wait=True)
            yield from abs_set(dexela.cam.num_images, xnum, wait=True)

        ion = flying_zebra.sclr
        yield from abs_set(ion.nuse_all,xnum)

        # arm the Zebra (start caching x positions)
        yield from kickoff(flying_zebra,
                           xstart=xstart, xstop=xstop, xnum=xnum, dwell=dwell,
                           wait=True)

        # arm SIS3820, note that there is a 1 sec delay in setting X
        # into motion so the first point *in each row* won't
        # normalize...
        yield from abs_set(ion.erase_start, 1)

        # trigger all of the detectors
        for d in flying_zebra.detectors:
            yield from bps.trigger(d, group='row')

        yield from bps.sleep(1.5)
        # start the 'fly'
        yield from abs_set(xmotor, xstop + 1*delta, group='row')  # move in x
        # wait for the motor and detectors to all agree they are done
        yield from bps.wait(group='row')

        # yield from abs_set(xs.settings.acquire, 0)  # stop acquiring images

        # we still know about ion from above
        yield from abs_set(ion.stop_all, 1)  # stop acquiring scaler

        yield from complete(flying_zebra)  # tell the Zebra we are done
        yield from collect(flying_zebra)  # extract data from Zebra
        # TODO what?
        if ('e_tomo' in xmotor.name):
            v_return = min(4, xmotor.velocity.high_limit)
            yield from bps.mov(xmotor.velocity, v_return)
        else:
            # set the "stage speed"
            yield from bps.mov(xmotor.velocity, 1.0)
            # TODO wat
            # set the "stage speed" twice just in case
            yield from abs_set(xmotor.velocity, 1.0, wait=True)

    def at_scan(name, doc):
        scanrecord.current_scan.put(doc['uid'][:6])
        scanrecord.current_scan_id.put(str(doc['scan_id']))
        scanrecord.current_type.put(md['scaninfo']['type'])
        scanrecord.scanning.put(True)
        scanrecord.time_remaining.put((dwell*xnum + 3.8)/3600)

    def finalize_scan(name, doc):
        logscan_detailed('xrf_fly')
        scanrecord.scanning.put(False)
        scanrecord.time_remaining.put(0)

    # TODO remove this eventually?
    xs = dets_by_name['xs']
    # xs = dets_by_name['xs2']

    # @subs_decorator([LiveTable([ymotor]),
    #                  RowBasedLiveGrid((ynum, xnum), ion.name, row_key=ymotor.name),
    #                  LiveZebraPlot()])
    # @subs_decorator([LiveTable([ymotor]), LiveGrid((ynum, xnum), sclr1.mca1.name)])
    if (ynum == 1):
        livepopup = LivePlot(xs.channel1.rois.roi01.value.name,
                             xlim=(xstart, xstop))
    else:
        livepopup = LiveGrid((ynum, xnum+1),
                             xs.channel1.rois.roi01.value.name,
                             extent=(xstart, xstop, ystart, ystop),
                             x_positive='right', y_positive='down')
    @subs_decorator([livepopup])
    # @subs_decorator([LiveGrid((ynum, xnum+1),
    #                           xs.channel1.rois.roi01.value.name,
    #                           extent=(xstart, xstop, ystart, ystop),
    #                           x_positive='right', y_positive='down')])
    @subs_decorator({'start': at_scan})
    @subs_decorator({'stop': finalize_scan})
    # monitor values from xs
    @monitor_during_decorator([xs.channel1.rois.roi01.value])
    @stage_decorator([flying_zebra])  # Below, 'scan' stage ymotor.
    @run_decorator(md=md)
    def plan():
        # TODO move this to stage sigs
        for d in flying_zebra.detectors:
            yield from bps.mov(d.total_points, xnum)
        # added to "prime" the detector
        #yield from abs_set(xs.settings.trigger_mode, 'TTL Veto Only')

        # TODO move this to stage sigs
        yield from bps.mov(xs.external_trig, True)
        ystep = 0

        for step in np.linspace(ystart, ystop, ynum):
            yield from abs_set(scanrecord.time_remaining,
                               (ynum - ystep) * ( dwell * xnum + 3.8 ) / 3600.)
            ystep = ystep + 1
            # 'arm' the all of the detectors for outputting fly data
            for d in flying_zebra.detectors:
                yield from bps.mov(d.fly_next, True)
            # print('h5 armed\t',time.time())
            yield from fly_each_step(ymotor, step)
            # print('return from step\t',time.time())

        # TODO this should be taken care of by stage sigs
        ion = flying_zebra.sclr
        yield from bps.mov(xs.external_trig, False,
                           ion.count_mode, 1)

    if shutter:
        final_plan = finalize_wrapper(plan(),
                                      bps.mov(shut_b, 'Close'))
    else:
        final_plan = plan()

    return (yield from final_plan)


def scan_and_fly(*args, extra_dets=None, **kwargs):
    kwargs.setdefault('xmotor', hf_stage.x)
    kwargs.setdefault('ymotor', hf_stage.y)
    _xs = kwargs.pop('xs', xs)
    kwargs.setdefault('flying_zebra', flying_zebra)
    # _xs = kwargs.pop('xs2', xs2)
    # kwargs.setdefault('flying_zebra', flying_zebra_x_xs2)
    # extra_dets = [xs2]
    if extra_dets is None:
        extra_dets = []
    dets = [_xs] + extra_dets
    # To fly both xs and merlin
    # yield from scan_and_fly_base([_xs, merlin], *args, **kwargs)
    # To fly only xs
    yield from scan_and_fly_base(dets, *args, **kwargs)


class RowBasedLiveGrid(LiveGrid):
    """
    Synthesize info from two event stream here.
    Use the event with 'row_key' in it to figure out when we have moved to a new row.
    Figure out if the seq_num has the right value, given the expected raster_shape.
    If seq_num is low, we have missed some values. Pad the seq_num (effectively leaving
    empty tiles at the end of the row) for future events.
    """
    def __init__(self, *args, row_key, **kwargs):
        super().__init__(*args, **kwargs)
        self._row_key = row_key
        self._last_row = None
        self._column_counter = None  # count tiles we have seen in current row
        self._pad = None
        self._desired_columns = self.raster_shape[1]  # number of tiles row should have

    def start(self, doc):
        super().start(doc)
        self._column_counter = 0
        self._last_row = None
        self._pad = 0

    def event(self, doc):
        # If this is an event that tells us what row we are in:
        if self._row_key in doc['data']:
            this_row = doc['data'][self._row_key]
            if self._last_row is None:
                # initialize with the first y value we see
                self._last_row = this_row
                return
            if this_row != self._last_row:
                # new row -- pad future sequence numbers if row fell short
                missing = self._desired_columns - self._column_counter
                self._pad += missing
                self._last_row = this_row
                self._column_counter = 0
        # If this is an event with the data we want to plot:
        if self.I in doc['data']:
            self._column_counter += 1
            doc = doc.copy()
            doc['seq_num'] += self._pad
            super().event(doc)

    def stop(self, doc):
        self._last_row = None
        self._column_counter = None
        self._pad = None


class SrxXSP3Handler:
    XRF_DATA_KEY = 'entry/instrument/detector/data'

    def __init__(self, filepath, **kwargs):
        self._filepath = filepath

    def __call__(self, **kwargs):
        with h5py.File(self._filepath, 'r') as f:
            return np.asarray(f[self.XRF_DATA_KEY])


def batch_fly(paramlist, kwlist=None, zlist=None):
    '''
    paramlist   list    list of positional and dwell time arguments to scan_and_fly
    kwlist      list    list of dicts containing keywords to pass to scan_and_fly
    '''

    if kwlist == None:
        kwlist=list()
        for _ in paramlist:
            kwlist.append({})

    for i in range(0,len(paramlist)):
        #this should be made more general
        if zlist is not None:
            yield from abs_set(hf_stage.z,zlist[i],wait=True)
        yield from scan_and_fly(*paramlist[i],**kwlist[i])


def batch_fly_arb(paramlist, kwlist=None, motlist=None):
    '''
    paramlist   list            list of positional and dwell time arguments to scan_and_fly
    kwlist      list            list of dicts containing keywords to pass to scan_and_fly
    motlist     list of lists   a list of motor,value pairs that define the prestart condition
    '''

    if kwlist is None:
        kwlist = list()
        for _ in paramlist:
            kwlist.append({})

#    for i in range(0,len(paramlist)):
#        if motlist is not None:
#            yield from abs_set(motlist[i][0],motlist[i][1],wait=True)
#        yield from scan_and_fly(*paramlist[i],**kwlist[i])
    for i in range(0, len(paramlist)):
        if motlist is not None:
            for pospair in motlist[i]:
                yield from abs_set(pospair[0], pospair[1], wait=True)
        yield from scan_and_fly(*paramlist[i], **kwlist[i])


def y_scan_and_fly(*args, **kwargs):
    kwargs.setdefault('xmotor', hf_stage.y)
    kwargs.setdefault('ymotor', hf_stage.x)
    '''
    convenience wrapper for scanning Y as the fast axis.
    call scan_and_fly, forcing slow and fast axes to be X and Y.
    in this function, the first three scan parameters are for the *fast axis*,
    i.e., the vertical, and the second three for the *slow axis*, horizontal.
    '''
    if 'delta' in kwargs.keys():
        # if kwargs['delta'] is not None:  # If delta is set in the arguments,
                                           # then we should not override that value
                                           # AMK
        if kwargs['delta'] is None:
            # kwargs['delta'] = 0.004        # default value
            v = (xstop - xstart) / (xnum-1) / dwell  # compute "stage speed"
            t_acc = 1.0  # acceleration time, default 1.0 s
            kwargs['delta'] = t_acc * v  # distance the stage will travel in t_acc

    yield from scan_and_fly(*args, **kwargs,
                            flying_zebra=flying_zebra_y)
    # yield from scan_and_fly(*args, **kwargs,
    #                         flying_zebra=flying_zebra_y_xs2)


def y_scan_and_fly_xs2(*args, **kwargs):
    '''
    convenience wrapper for scanning Y as the fast axis.
    call scan_and_fly, forcing slow and fast axes to be X and Y.
    in this function, the first three scan parameters are for the *fast axis*,
    i.e., the vertical, and the second three for the *slow axis*, horizontal.
    A copy of flying_zebra_y where the xspress3 mini is chosen to collect data.
    '''

    if 'delta' in kwargs.keys():
        # if kwargs['delta'] is not None:  # If delta is set in the arguments,
                                           # then we should not override that value
                                           # AMK
        if kwargs['delta'] is None:
            # kwargs['delta'] = 0.004        # default value
            v = (xstop - xstart) / (xnum-1) / dwell  # compute "stage speed"
            t_acc = 1.0  # acceleration time, default 1.0 s
            kwargs['delta'] = t_acc * v  # distance the stage will travel in t_acc

    yield from scan_and_fly(*args, **kwargs,
                            xmotor=hf_stage.y,
                            ymotor=hf_stage.x,
                            # xmotor=e_tomo.y,
                            # ymotor=e_tomo.x,
                            flying_zebra=flying_zebra_y_xs2,
                            xs=xs2)

def y_scan_and_fly_xs2_yz(*args, **kwargs):
    '''
    convenience wrapper for scanning Y as the fast axis.
    ** This is a variant of y_scan_and_fly_xs2 but with Z and the slow motor (not X) ***
    call scan_and_fly, forcing slow and fast axes to be Z and Y.
    in this function, the first three scan parameters are for the *fast axis*,
    i.e., the vertical, and the second three for the *slow axis*, horizontal.
    A copy of flying_zebra_y where the xspress3 mini is chosen to collect data.
    '''

    if 'delta' in kwargs.keys():
        # if kwargs['delta'] is not None:  # If delta is set in the arguments,
                                           # then we should not override that value
                                           # AMK
        if kwargs['delta'] is None:
            # kwargs['delta'] = 0.004        # default value
            v = (xstop - xstart) / (xnum-1) / dwell  # compute "stage speed"
            t_acc = 1.0  # acceleration time, default 1.0 s
            kwargs['delta'] = t_acc * v  # distance the stage will travel in t_acc

    yield from scan_and_fly(*args, **kwargs,
                            xmotor=hf_stage.y,
                            ymotor=hf_stage.z,
                            # xmotor=e_tomo.y,
                            # ymotor=e_tomo.x,
                            flying_zebra=flying_zebra_y_xs2,
                            xs=xs2)


def scan_and_fly_xs2(*args, **kwargs):
    '''
    A copy of flying_zebra where the xspress3 mini is chosen to collect data on the X axis
    '''

    if 'delta' in kwargs.keys():
        # if kwargs['delta'] is not None:  # If delta is set in the arguments,
                                           # then we should not override that value
                                           # AMK
        if kwargs['delta'] is None:
            # kwargs['delta'] = 0.004        # default value
            v = (xstop - xstart) / (xnum-1) / dwell  # compute "stage speed"
            t_acc = 1.0  # acceleration time, default 1.0 s
            kwargs['delta'] = t_acc * v  # distance the stage will travel in t_acc

    yield from scan_and_fly(*args, **kwargs,
                            xmotor=hf_stage.x,
                            ymotor=hf_stage.y,
                            # xmotor=e_tomo.x,
                            # ymotor=e_tomo.y,
                            flying_zebra=flying_zebra_x_xs2,
                            xs=xs2)

def scan_and_fly_xs2_xz(*args, **kwargs):
    '''
    A copy of flying_zebra where the xspress3 mini is chosen to collect data on the X axis
    '''

    if 'delta' in kwargs.keys():
        # if kwargs['delta'] is not None:  # If delta is set in the arguments,
                                           # then we should not override that value
                                           # AMK
        if kwargs['delta'] is None:
            # kwargs['delta'] = 0.004        # default value
            v = (xstop - xstart) / (xnum-1) / dwell  # compute "stage speed"
            t_acc = 1.0  # acceleration time, default 1.0 s
            kwargs['delta'] = t_acc * v  # distance the stage will travel in t_acc

    yield from scan_and_fly(*args, **kwargs,
                            xmotor=hf_stage.x,
                            ymotor=hf_stage.z,
                            # xmotor=e_tomo.x,
                            # ymotor=e_tomo.y,
                            flying_zebra=flying_zebra_x_xs2,
                            xs=xs2)
# © 2019 GitHub, Inc.
# Terms
# Privacy
# Security
# Status
# Help
# Contact GitHub
# Pricing
# API
# Training
# Blog
# About
