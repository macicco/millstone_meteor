#!/usr/bin/env python

"""A script to run on Digital RF data to detect and summarize meteors."""
import numpy as np
import pandas as pd
import xarray as xr
import scipy as sp
import scipy.stats
import os
from collections import namedtuple
import math
import csv

from time_utils import datetime_to_float
import digital_rf_hdf5 as drf
import digital_metadata as dmd
import TimingModeManager
import meteor_processing as mp
from clustering import Clustering
import meteor_plotting
from valarg import valargmax

noise_pwr_rv = sp.stats.chi2(2)
#med_pwr_est_factor = noise_pwr_rv.mean()/noise_pwr_rv.median()
med_pwr_est_factor = noise_pwr_rv.mean()/noise_pwr_rv.ppf(
                         0.5*noise_pwr_rv.cdf(3*noise_pwr_rv.median()))

def interval_range(start, stop, step=1):
    iterable = iter(xrange(start, stop, step))
    a = b = iterable.next()
    for b in iterable:
        yield (a, b)
        a = b
    yield (b, stop)

def pulse_generator(ido, no, tmm, s0, s1, nch, ds=None):
    fs = ido.get_samples_per_second()
    if ds is None:
        # needs to be long enough to get at least one noise metadata sample
        ds = int(5*fs)

    rasters = {}
    ntattr = 'temp_{0}_median'.format(nch)
    npattr = '{0}_off_100_kHz_median'.format(nch)

    for ss, se in interval_range(s0, s1, ds):
        noisemd = xr.Dataset.from_dataframe(
            pd.DataFrame.from_dict(no.read(ss-ds, se, [ntattr, npattr]), orient='index')
        )

        idmd = ido.read(ss, se, 'sweepid')
        for sp, sweepid in idmd.iteritems():
            # skip all but uncoded pulses for now
            #if sweepid != 300:
                #continue
            try:
                raster_table = rasters[sweepid]
            except KeyError:
                sweep = tmm.getTimingSweepByID(sweepid)
                code = sweep.getCodeObject()
                raster_table = code.getRasterTable()
                rasters[sweepid] = raster_table

            offset = -int(np.round(raster_table['full'][0]*fs/1e9))

            noise_attrs = noisemd.sel(index=sp, method='ffill').data_vars
            noise_dict = dict(
                noise_temp=noise_attrs[ntattr].item(),
                noise_power=noise_attrs[npattr].item()*med_pwr_est_factor*fs/100e3,
            )

            yield sp+offset, sweepid, raster_table, noise_dict

def data_generator(rfo, ido, no, tmm, s0, s1, rxch, txch):
    rx_attrs = {k: v.value for k, v, in rfo.get_metadata(rxch).iteritems()}
    tx_attrs = {k: v.value for k, v, in rfo.get_metadata(txch).iteritems()}

    rxfs = rx_attrs['sample_rate']
    txfs = tx_attrs['sample_rate']

    nch = rxch.split('-')[0]

    for s, sweepid, raster, noise in pulse_generator(ido, no, tmm, s0, s1, nch):
        t = np.round(s * (1e9/rxfs)).astype('datetime64[ns]')

        tx_raster = (int(np.round(raster['tx'][0]*txfs/1e9)),
                     int(np.round(raster['tx'][1]*txfs/1e9)))
        tx_data = rfo.read_vector_c81d(int(np.round(s*txfs/rxfs)) + tx_raster[0],
                                       tx_raster[1] - tx_raster[0], txch)

        rx_raster = (int(np.round(raster['blank'][1]*rxfs/1e9)),
                     int(np.round(raster['signal'][1]*rxfs/1e9)))
        rx_data = rfo.read_vector_c81d(s + rx_raster[0],
                                       rx_raster[1] - rx_raster[0], rxch)

        tx_attrs.update(sweepid=sweepid)
        tx = xr.DataArray(
            tx_data,
            coords=dict(t=t,
                        delay=('delay', np.arange(tx_raster[0], tx_raster[1]),
                               {'label': 'Delay (samples)'}),
                   ),
            dims=('delay',),
            name='tx',
            attrs=tx_attrs,
        )

        rx_attrs.update(noise)
        rx = xr.DataArray(
            rx_data,
            coords=dict(t=t,
                        delay=('delay', np.arange(rx_raster[0], rx_raster[1]),
                               {'label': 'Delay (samples)'}),
                   ),
            dims=('delay',),
            name='rx',
            attrs=rx_attrs,
        )

        yield tx, rx

def detect_meteors(rf_dir, id_dir, noise_dir, output_dir,
                   t0=None, t1=None, rxch='zenith-l', txch='tx-h', vmin=7, vmax=72, snr_thres=1, eps=15, min_samples=1, tscale=0.03, rscale=150, vscale=710.27, rmin=70, rmax=140, filename="cluster_summaries.txt"):
    """Function to detect and summarize meteor head echoes.


    Arguments
    ---------

    rf_dir : string or list
        RF data directory or directories.

    id_dir : string
        ID code metadata directory.

    noise_dir : string
        RX noise metadata directory.

    output_dir : string
        Meteor data output directory.

    t0 : float, optional
        Start time, seconds since epoch. If None, start at beginning of data.

    t1 : float, optional
        End time, seconds since epoch. If None, end at end of data.

    rxch : string, optional
        Receiver channel to process.

    txch : string, optional
        Transmitter channel.

    """
    rfo = drf.read_hdf5(rf_dir)
    ido = dmd.read_digital_metadata(id_dir)
    no = dmd.read_digital_metadata(noise_dir)

    if t0 is None or t1 is None:
        bounds = []
        bounds.append(rfo.get_bounds(rxch))
        bounds.append(rfo.get_bounds(txch))
        bounds.append(ido.get_bounds())
        bounds.append(no.get_bounds())
        bounds = np.asarray(bounds)

        ss = np.max(bounds[:, 0])
        se = np.min(bounds[:, 1])

        fs = rfo.get_metadata(rxch)['sample_rate'].value

        if t0 is None:
            s0 = ss
        else:
            s0 = int(np.round(t0*fs))

        if t1 is None:
            s1 = se
        else:
            s1 = int(np.round(t1*fs))

    tmm = TimingModeManager.TimingModeManager()
    if os.path.exists('/tmp/tmm.hdf5'):
        tmm.loadFromHdf5('/tmp/tmm.hdf5', skip_lowlevel=True)
    else:
        tmm.loadFromHdf5(skip_lowlevel=True)

    pulse_data = data_generator(rfo, ido, no, tmm, s0, s1, rxch, txch)
    clustering = Clustering(eps, min_samples, tscale, rscale, vscale)
    cols = ["duration", "inital r", "initial t", "lstsq", "overall range rate", "range rates", "range rates var", "snr mean", "snr peak", "snr var"]
    with open(filename, "w") as csvfile:
        w = csv.DictWriter(csvfile, cols)
	w.writeheader()

    csvfile = open(filename, "ab")
    cluster_list = []

    def clust(data):
	for c in clustering.addnext(t=data[0], r=data[1], v=data[3], snr=data[4], pulse_num=data[5]):
            yield c

    for k, (tx, rx) in enumerate(pulse_data):

        mf_rx = mp.matched_filter(tx, rx, rmin, rmax)
	snr_vals = (np.abs(mf_rx.values)**2)/rx.noise_power
        max_snr = valargmax(snr_vals)[0]
        snr_point = np.unravel_index(valargmax(snr_vals)[1], (mf_rx.shape[0], mf_rx.shape[1]))
        meteor_list = mp.is_there_a_meteor(mf_rx, max_snr, snr_point, snr_thres, vmin, vmax, k, rx.sample_rate)

	if meteor_list != []:
	    print k
	    c = list(clust(meteor_list))
            if c != []:
	        cluster_list.append(c)
                for item in c:
	            cluster_summary1 = mp.summary(item)
                    cluster_summary1.to_csv(csvfile, header=False, index=False)

    def cluster_finish():
        for c in clustering.finish():
            yield c
    
    clusters = list(cluster_finish())
    cluster_list.append(clusters)
    for item in clusters:
        cluster_summary = mp.summary(item)
        cluster_summary.to_csv(csvfile, header=False, index=False)
    return cluster_list, rx

if __name__ == "__main__":
    from argparse import ArgumentParser

    parser = ArgumentParser()

    parser.add_argument(
        'rf_dir', nargs='+',
        help='RF Data directory or directories.',
    )
    parser.add_argument(
        '-i', '--id_dir', required=True,
        help='ID code metadata directory.',
    )
    parser.add_argument(
        '-n', '--noise_dir', required=True,
        help='RX noise metadata directory.',
    )
    parser.add_argument(
        '-o', '--output_dir', required=True,
        help='Meteor data output directory.',
    )
    parser.add_argument(
        '-0', '--t0', type=float, default=None,
        help='Start time, seconds since epoch. (default: beginning of data)',
    )
    parser.add_argument(
        '-1', '--t1', type=float, default=None,
        help='End time, seconds since epoch. (default: end of data)',
    )
    parser.add_argument(
        '-c', '--rxch', default='zenith-l',
        help='Receiver channel to process.',
    )
    parser.add_argument(
        '-t', '--txch', default='tx-h',
        help='Transmitter channel.',
    )

    args = parser.parse_args()

cluster_list, rx = detect_meteors(args.rf_dir, args.id_dir, args.noise_dir, args.output_dir,
                   args.t0, args.t1, args.rxch, args.txch)


