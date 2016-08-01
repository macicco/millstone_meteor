import numpy as np
import math
import xarray as xr
import pandas as pd
from scipy.constants import c

from time_utils import datetime_to_float, datetime_from_float
import rkl

def matched_filter(tx, rx, rmin_km, rmax_km):
    """Frequency bank of matched filters for a single pulse.

    Filter rx with tx, keeping data only from ranges rmin_km to rmax_km.

    Output mf_rx = 2D array (delay, frequency).

    """
    fs = rx.sample_rate
    fc = rx.center_frequencies

    # convert range bounds to bounds on the delay in number of samples
    delay_min = int(np.floor((2*fs*rmin_km*1000)/c))
    delay_max = int(np.ceil((2*fs*rmax_km*1000)/c))

    # indexes into rx array that correspond to desired delay window
    rx_start = max(delay_min - rx.delay.values[0], 0)
    # need len(tx) samples past delay_max so full correlation can be done
    rx_stop = min(delay_max + tx.shape[0] - rx.delay.values[0], rx.shape[0])

    rx_corr = rx.values[rx_start:rx_stop]
    # normalize tx data so that noise level is the same after matched filtering
    tx_normalized = tx.values/np.linalg.norm(tx.values)

    # perform matched filter calculation as (delay and multiply) + (FFT)
    y = rkl.delay_multiply.delaymult_like_arg2(rx_corr, tx_normalized, R=1)
    z = np.fft.fft(y)
    # select matched filtered values that have full correlation with tx only
    # by removing len(tx)-1 partial correlation values from beginning and end
    L = tx.shape[0] - 1
    z_valid = z[L:-L, :]

    # calculate coordinates for matched filtered data
    delays = np.arange(rx_start + rx.delay.values[0],
                          rx_stop - tx.shape[0] + rx.delay.values[0])
    ranges = delay_idx*c/(2*fs)
    freqs = np.fft.fftfreq(tx.shape[0], fs)
    vels = -freq_idx*c/(2*fc) # positive frequency shift is negative range rate

    mf_rx = xr.DataArray(
        z_valid,
        coords=dict(
            t=rx.t.values,
            delay=('delay', delays, {'label': 'Delay (samples)'}),
            range=('delay', ranges, {'label': 'Range (m)'}),
            frequency=('frequency', freqs, {'label': 'Doppler frequency shift (Hz)'}),
            range_rate=('frequency', vels, {'label': 'Range rate (m/s)'}),
        ),
        dims=('delay', 'frequency',),
        name='mf_rx',
        attrs=rx.attrs,
    )
    return mf_rx

# meteor signal detection for a single pulse
# need to include range in output
def detect_meteors(mf_rx, snr_thresh, vmin_kps, vmax_kps):
    """Meteor signal detection for a single pulse.

    Returns a list of detected meteor points in time-range-velocity space.

    """
    snr_vals = (mf_rx.real**2 + mf_rx.imag**2)/mf_rx.noise_power

    # for now, only detect based on highest SNR point in delay-frequency space
    snr, snr_idx = valargmax(snr_vals)
    delay_idx, freq_idx = np.unravel_index(snr_idx, snr_vals.shape)
    v = mf_rx.range_rate.values[freq_idx]

    # detection conditions based on SNR threshold and velocity window
    # v is negative and m/s, vmin and vmax are positive and km/s
    if snr >= snr_thresh and -vmax_kps <= v/1e3 <= -vmin_kps:
        meteor = dict(
            t=datetime_to_float(mf_rx.t.values),
            r=mf_rx.range.values[delay_idx],
            v=v,
            snr=snr,
        )
        return [meteor]
    return None

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
