# -*- coding: utf-8 -*-
"""
Created on Wed Apr  5 14:02:41 2023 by Guido Meijer
"""

import os
from os.path import join, split, isfile, isdir, dirname, realpath
import numpy as np
import pandas as pd
from datetime import datetime
import shutil
from glob import glob
from pathlib import Path
import json
import matplotlib.pyplot as plt

from ibllib.ephys import ephysqc
from ibllib.ephys.spikes import ks2_to_alf, sync_spike_sorting
from ibllib.pipes.ephys_tasks import (EphysCompressNP1, EphysSyncPulses, EphysSyncRegisterRaw,
                                      EphysPulses)
from brainbox.metrics.single_units import spike_sorting_metrics
from phylib.io.model import get_template_params
from phylib.utils._misc import write_python

import spikeinterface.full as si

from powerpixel_utils import load_neural_data

# Load in setting files
with open(join(dirname(realpath(__file__)), 'settings.json'), 'r') as openfile:
    settings_dict = json.load(openfile)
with open(join(dirname(realpath(__file__)), 'wiring_files', 'nidq.wiring.json'), 'r') as openfile:
    nidq_sync_dictionary = json.load(openfile)
with open(join(dirname(realpath(__file__)), 'wiring_files',
               f'{nidq_sync_dictionary["SYSTEM"]}.wiring.json'), 'r') as openfile:
    probe_sync_dictionary = json.load(openfile)

# Get run identifier string
if len(settings_dict['IDENTIFIER']) > 0:
    id_str = '_' + settings_dict['IDENTIFIER']
else:
    id_str = ''
    
# Load in spike sorting parameters
if isfile(join(dirname(realpath(__file__)), 'spikesorter_param_files',
               f'{settings_dict["SPIKE_SORTER"]}_params.json')):
    with open(join(dirname(realpath(__file__)), 'spikesorter_param_files',
                   f'{settings_dict["SPIKE_SORTER"]}_params.json'), 'r') as openfile:
        sorter_params = json.load(openfile)
else:
    sorter_params = si.get_default_sorter_params(settings_dict['SPIKE_SORTER'])

# Initialize Matlab engine for bombcell package
if settings_dict['RUN_BOMBCELL']:
    import matlab.engine
    eng = matlab.engine.start_matlab()
    eng.addpath(r"{}".format(os.path.dirname(os.path.realpath(__file__))), nargout=0)
    eng.addpath(eng.genpath(settings_dict['BOMBCELL_PATH']))
    eng.addpath(eng.genpath(settings_dict['MATLAB_NPY_PATH']))

# Search for process_me.flag
print('Looking for process_me.flag..')
for root, directory, files in os.walk(settings_dict['DATA_FOLDER']):
    if 'process_me.flag' in files:
        session_path = Path(root)
        print(f'\nStarting pipeline in {root} at {datetime.now().strftime("%H:%M")}\n')
        
        # Restructure file and folders
        if 'probe00' not in os.listdir(join(root, 'raw_ephys_data')):
            if len(os.listdir(join(root, 'raw_ephys_data'))) == 0:
                print('No ephys data found')
                continue
            elif len(os.listdir(join(root, 'raw_ephys_data'))) > 1:
                print('More than one run found, not supported')
                continue
            orig_dir = os.listdir(join(root, 'raw_ephys_data'))[0]
            for i, this_dir in enumerate(os.listdir(join(root, 'raw_ephys_data', orig_dir))):
                shutil.move(join(root, 'raw_ephys_data', orig_dir, this_dir),
                            join(root, 'raw_ephys_data'))
            os.rmdir(join(root, 'raw_ephys_data', orig_dir))
            for i, this_path in enumerate(glob(join(root, 'raw_ephys_data', '*imec*'))):
                os.rename(this_path, join(root, 'raw_ephys_data', 'probe0' + this_path[-1]))
                
        # Create synchronization file
        nidq_file = next(session_path.joinpath('raw_ephys_data').glob('*.nidq.*bin'))
        with open(nidq_file.with_suffix('.wiring.json'), 'w') as fp:
            json.dump(nidq_sync_dictionary, fp, indent=1)
        
        for ap_file in session_path.joinpath('raw_ephys_data').rglob('*.ap.cbin'):
            with open(ap_file.with_suffix('.wiring.json'), 'w') as fp:
                json.dump(probe_sync_dictionary, fp, indent=1)
        
        # Create nidq sync file        
        EphysSyncRegisterRaw(session_path=session_path, sync_collection='raw_ephys_data').run()
                
        # Loop over multiple probes 
        probes = glob(join(root, 'raw_ephys_data', 'probe*'))
        probe_done = np.zeros(len(probes)).astype(bool)
        for i, probe_path in enumerate(probes):
            print(f'\nStarting preprocessing of {split(probe_path)[-1]}')
            
            # Check if probe is already processed
            this_probe = split(probe_path)[1]
            if isdir(join(root, this_probe + id_str)):
                print('Probe already processed, moving on')
                probe_done[i] = True
                continue
            
            # Create probe sync file
            task = EphysSyncPulses(session_path=session_path, sync='nidq', pname=this_probe,
                                   sync_ext='bin', sync_namespace='spikeglx',
                                   sync_collection='raw_ephys_data',
                                   device_collection='raw_ephys_data')
            task.run()
            task = EphysPulses(session_path=session_path, pname=this_probe,
                               sync_collection='raw_ephys_data',
                               device_collection='raw_ephys_data')
            task.run()
           
            # Load in recording
            if len(glob(join(probe_path, '*.cbin'))) > 0:
                # Recording is already compressed by a previous run, loading in compressed data
                rec = si.read_cbin_ibl(probe_path)
            else:
                rec = si.read_spikeglx(probe_path, stream_id=f'imec{split(probe_path)[-1][-1]}.ap')
            
            # Apply high-pass filter
            print('\nApplying high-pass filter.. ')
            rec_filtered = si.highpass_filter(rec, ftype='bessel', dtype='float32')
                        
            # Correct for inter-sample phase shift
            print('Correcting for phase shift.. ')
            rec_shifted = si.phase_shift(rec_filtered)
            
            # Do common average referencing
            print('Performing common average referencing.. ')
            rec_comref = si.common_reference(rec_filtered)
            
            print('Detecting and interpolating over bad channels.. ')
            
            # Detect dead channels
            bad_channel_ids, all_channels = si.detect_bad_channels(rec_filtered, seed=42)
            prec_dead_ch = np.sum(all_channels == 'dead') / all_channels.shape[0]
            print(f'{np.sum(all_channels == "dead")} ({prec_dead_ch*100:.0f}%) dead channels')
            dead_channel_ids = rec_filtered.get_channel_ids()[all_channels == 'dead']
            prec_out_ch = np.sum(all_channels == 'out') / all_channels.shape[0]
            print(f'{np.sum(all_channels == "out")} ({prec_out_ch*100:.0f}%) channels outside of the brain')
            out_channel_ids = rec_filtered.get_channel_ids()[all_channels == 'out']
            
            # Detect noisy channels
            bad_channel_ids, all_channels = si.detect_bad_channels(rec_comref, method='mad',
                                                                   std_mad_threshold=3, seed=42)
            prec_noise_ch = np.sum(all_channels == 'noise') / all_channels.shape[0]
            print(f'{np.sum(all_channels == "noise")} ({prec_noise_ch*100:.0f}%) noise channels')
            noisy_channel_ids = rec_comref.get_channel_ids()[all_channels == 'noise']
            
            # Interpolate over bad channels          
            rec_interpolated = si.interpolate_bad_channels(rec_shifted, np.concatenate((
                dead_channel_ids, noisy_channel_ids, out_channel_ids)))
                        
            # If there are multiple shanks, do destriping per shank
            print('Destriping.. ')
            if np.unique(rec_interpolated.get_property('group')).shape[0] > 1:
                
                # Loop over shanks and do preprocessing per shank
                rec_split = rec_interpolated.split_by(property='group')
                rec_destriped = []
                for sh in range(len(rec_split)):
                    rec_destriped.append(si.highpass_spatial_filter(rec_split[sh]))
                
                # Merge back together
                rec_destriped = si.aggregate_channels(
                    rec_destriped, renamed_channel_ids=rec_interpolated.get_channel_ids())
                
            else:
                
                # Do destriping for the whole probe at once
                rec_destriped = si.highpass_spatial_filter(rec_interpolated)
            
            # Plot spectral density
            print('Calculating power spectral density')
            data_chunk = si.get_random_data_chunks(rec_destriped, num_chunks_per_segment=1,
                                                   chunk_size=30000, seed=42)
            fig, ax = plt.subplots(figsize=(10, 7))
            for tr in data_chunk.T:
                p, f = ax.psd(tr, Fs=rec_destriped.sampling_frequency, color="b")
            plt.savefig(join(probe_path, 'power spectral density.jpg'), dpi=600)
            
            # Apply notch filter 
            if isfile(join(probe_path, 'notch_filter.json')):
                
                # Load in notch filter settings
                with open(join(probe_path, 'notch_filter.json'), 'r') as openfile:
                    notch_filter = json.load(openfile)
                    
                # Apply filters
                rec_notch = rec_destriped
                for freq, q in zip(notch_filter['FREQ'], notch_filter['Q']):
                    print(f'Applying notch filter at {freq} Hz..')
                    rec_notch = si.notch_filter(rec_notch, freq=freq, q=q)
                    
                # Plot spectral density
                print('Calculating power spectral density')
                data_chunk = si.get_random_data_chunks(rec_notch, num_chunks_per_segment=1,
                                                       chunk_size=30000, seed=42)
                fig, ax = plt.subplots(figsize=(10, 7))
                for tr in data_chunk.T:
                    p, f = ax.psd(tr, Fs=rec_destriped.sampling_frequency, color="b")
                plt.savefig(join(probe_path, 'power spectral density after notch filter.jpg'), dpi=600)
                
                rec_final = rec_notch
                
            else:
                rec_final = rec_destriped
                
            # Run spike sorting
            try:
                print(f'\nStarting {split(probe_path)[-1]} spike sorting at {datetime.now().strftime("%H:%M")}')
                sort = si.run_sorter(
                    settings_dict['SPIKE_SORTER'],
                    rec_final,
                    output_folder=join(probe_path, settings_dict['SPIKE_SORTER'] + id_str),
                    verbose=True,
                    docker_image=settings_dict['USE_DOCKER'],
                    **sorter_params)
            except Exception as err:
                
                # Log error to disk
                print(err)
                logf = open(os.path.join(probe_path, 'error_log.txt'), 'w')
                logf.write(str(err))
                logf.close()
                
                # Delete empty sorting directory
                shutil.rmtree(join(probe_path, settings_dict['SPIKE_SORTER'] + id_str))
                
                # Continue with next recording
                continue
            
            # Get AP and meta data files
            bin_path = Path(join(root, 'raw_ephys_data', this_probe))
            ap_file = glob(join(bin_path, '*ap.*bin'))[0]
            meta_file = glob(join(bin_path, '*ap.meta'))[0]
            
            # Compute raw ephys QC metrics
            if not isfile(join(probe_path, '_iblqc_ephysSpectralDensityAP.power.npy')):
                task = ephysqc.EphysQC('', session_path=session_path, use_alyx=False)
                task.probe_path = Path(probe_path)
                task.run()
            
            # Get folder paths
            sorter_out_path = Path(join(probe_path,
                                        settings_dict['SPIKE_SORTER'] + id_str,
                                        'sorter_output'))
            alf_path = Path(join(root, this_probe + id_str))
            
            # Run Bombcell
            if settings_dict['RUN_BOMBCELL']:
                print('Running Bombcell')
                eng.run_bombcell(str(sorter_out_path),
                                 ap_file,
                                 meta_file,  
                                 join(split(sorter_out_path)[0], 'bombcell_qc'),
                                 probe_path,
                                 nargout=0)
            
            # Change the pointer in phy to the raw data from the temporary recording.dat
            # to the original bin file
            phy_params = get_template_params(sorter_out_path / 'params.py')
            phy_params['dat_path'] = str(bin_path / ap_file)
            phy_params['dir_path'] = str(phy_params['dir_path'])
            phy_params['dtype'] = str(phy_params['dtype'])
            write_python(sorter_out_path / 'params.py', phy_params)
            
            # Export as alf
            if not isdir(alf_path):
                os.mkdir(alf_path)
            ks2_to_alf(sorter_out_path, bin_path, alf_path)
            
            # Move LFP power etc. to the alf folder
            qc_files = glob(join(bin_path, '_iblqc_*'))
            for ii, this_file in enumerate(qc_files):
                shutil.move(this_file, join(alf_path, split(this_file)[1]))
            
            # Add bombcell QC to alf folder
            if settings_dict['RUN_BOMBCELL']:
                shutil.copy(join(sorter_out_path, 'cluster_bc_unitType.tsv'),
                            join(alf_path, 'cluster_bc_unitType.tsv'))
                shutil.copy(join(sorter_out_path, 'cluster_frac_RPVs.tsv'),
                            join(alf_path, 'cluster_frac_RPVs.tsv'))
                shutil.copy(join(sorter_out_path, 'cluster_SNR.tsv'),
                            join(alf_path, 'cluster_SNR.tsv'))
                bc_unittype = pd.read_csv(join(alf_path, 'cluster_bc_unitType.tsv'), sep='\t')
                np.save(join(alf_path, 'clusters.bcUnitType.npy'), bc_unittype['bc_unitType'])
                
            # Calculate and add IBL quality metrics
            print('Calculating IBL neuron-level quality metrics..')
            spikes, clusters, channels = load_neural_data(root, this_probe, histology=False,
                                                          only_good=False)
            df_units, rec_qc = spike_sorting_metrics(spikes['times'], spikes['clusters'],
                                                     spikes['amps'], spikes['depths'],
                                                     cluster_ids=clusters['cluster_id'])
            df_units['ibl_label'] = df_units['label']
            df_units[['cluster_id', 'ibl_label']].to_csv(join(alf_path, 'cluster_IBLLabel.tsv'),
                                                     sep='\t', index=False)
            
            # Synchronize spike sorting to nidq clock
            sync_spike_sorting(Path(ap_file), alf_path)
            
            # Extract digital sync timestamps
            sync_times = np.load(join(root, 'raw_ephys_data', '_spikeglx_sync.times.npy'))
            sync_polarities = np.load(join(root, 'raw_ephys_data', '_spikeglx_sync.polarities.npy'))
            sync_channels = np.load(join(root, 'raw_ephys_data', '_spikeglx_sync.channels.npy'))
            for ii, ch_name in enumerate(nidq_sync_dictionary['SYNC_WIRING_DIGITAL'].keys()):
                if ch_name == 'imec_sync':
                    continue
                nidq_pulses = sync_times[(sync_channels == int(ch_name[-1])) & (sync_polarities == 1)]
                np.save(join(root, nidq_sync_dictionary['SYNC_WIRING_DIGITAL'][ch_name] + '.times.npy'),
                        nidq_pulses)
            
            # Delete copied recording.dat file
            if isfile(join(sorter_out_path, 'recording.dat')):
                os.remove(join(sorter_out_path, 'recording.dat'))
                
            """
            # Compress raw data
            if len(glob(join(root, 'raw_ephys_data', this_probe, '*ap.cbin'))) == 0:
                print('Compressing raw binary file')
                task = EphysCompressNP1(session_path=Path(root), pname=this_probe)
                task.run()
                
            # Delete original raw data
            if ((len(glob(join(root, 'raw_ephys_data', this_probe, '*ap.cbin'))) == 0)
                and (len(glob(join(root, 'raw_ephys_data', this_probe, '*ap.bin'))) == 1)):
                try:
                    os.remove(glob(join(root, 'raw_ephys_data', this_probe, '*ap.bin'))[0])
                except:
                    print('Could not remove uncompressed ap bin file, delete manually')
                    continue
            """
            
            probe_done[i] = True
            print(f'Done! At {datetime.now().strftime("%H:%M")}')
        
        # Delete process_me.flag if all probes are processed
        if np.sum(probe_done) == len(probes):
            os.remove(os.path.join(root, 'process_me.flag'))
