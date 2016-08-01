import numpy as np
import math
import xarray as xr
import pandas as pd

from time_utils import datetime_to_float, datetime_from_float
import rkl

# frequency bank of matched filters for a single pulse
# mf_rx = 2D array (delay, frequency)
def matched_filter(tx, rx, rmin, rmax):
    fs = rx.sample_rate
    delay_min = np.int((2*fs*rmin*1000)/3e8)
    delay_max = np.int((2*fs*rmax*1000)/3e8)

    rx_corr = rx.values[: delay_max - rx.delay.values[0] + 1 + tx.shape[0]]
    y = rkl.delay_multiply.delaymult_like_arg2(rx_corr, tx.values/np.sum(tx.values), R=1)
    z = np.fft.fft(y)
    f = (np.fft.fftfreq(tx.shape[0], 1e-6))*-1
    delay_array = np.arange(delay_min, delay_max)

    mf_rx = xr.DataArray(z[delay_min - (rx.delay.values[0] - tx.shape[0]) : delay_max - (rx.delay.values[0] - tx.shape[0]), :], coords=dict(t=rx.t.values, delay=('delay', delay_array, {'label': 'Delay (samples)'}), frequency=('frequency', f)), dims=('delay', 'frequency',), 
name='mf_rx')
    mf_rx.attrs['center_frequencies'] = rx.center_frequencies
    return mf_rx

def vel_to_freq(velocity):
    f = (2*velocity*1000*440e6)/(3e8)
    return f

# meteor signal detection for a single pulse
# need to include range in output
def is_there_a_meteor(data, snr_val, snr_idx, thres, vmin, vmax, pulse_num, fs):
    fmin = vel_to_freq(vmin)
    fmax = vel_to_freq(vmax)
    meteor_list = []
    if snr_val >= thres:
        if -1*fmax < data.frequency.values[snr_idx[1]] < -1*fmin:
            # returns object's time, range, frequency at max SNR
    	    t = datetime_to_float(data.t.values)
            signal_range = (3e8*data.delay.values[snr_idx[0]])/(2*fs)
            v = list((data.frequency.values[snr_idx[1]]*3e8)/(data.center_frequencies*2))[0]
            info = (t, signal_range, data.frequency.values[snr_idx[1]], v, snr_val, pulse_num)
            meteor_list.extend(info)
    return meteor_list 

# runs some statistics on the data and summaries them
def summary(events):
    d = {}
    d['initial t'] = datetime_from_float(events['t'][0], 'ms')
    t = events['t'][events['t'].shape[0] - 1] - events['t'][0]
    d['duration'] = t
    d['initial r'] = events['r'][0]
    d['overall range rate'] = (events['r'][0] - events['r'][events['r'].shape[0] - 1])/t
    d['snr mean'] = np.mean(events['snr'])
    d['snr var'] = np.var(events['snr'])
    d['snr peak'] = np.max(events['snr'])
    d['range rates'] = []
    d['range rates'].append(list(events['v'].values))
    d['range rates var'] = np.var(events['v'])
    A1 = np.append(np.ones(events['t'].shape[0]), np.zeros(events['r'].shape[0]))      
    A2 = np.append(events['t'] - events['t'][0], np.ones(events['r'].shape[0]))
    A = np.vstack([A1, A2]).T
    n = np.linalg.lstsq(A, np.append(events['r'], events['v']))
    d['lstsq'] = []
    d['lstsq'].append(n[0])
    cluster_summary = pd.DataFrame(d)
    return cluster_summary
