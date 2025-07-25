import numpy as np
import torch
import torch.nn as nn
import os
import argparse
import soundfile as sf

from demucs.states import load_model
from demucs import pretrained
from demucs.apply import apply_model
import onnxruntime as ort
from time import time
import librosa
import hashlib
import sys


__VERSION__ = '1.0.1'


class Conv_TDF_net_trim_model(nn.Module):
    def __init__(self, device, target_name, L, n_fft, hop=1024):

        super(Conv_TDF_net_trim_model, self).__init__()

        self.dim_c = 4
        self.dim_f, self.dim_t = 3072, 256
        self.n_fft = n_fft
        self.hop = hop
        self.n_bins = self.n_fft // 2 + 1
        self.chunk_size = hop * (self.dim_t - 1)
        self.window = torch.hann_window(window_length=self.n_fft, periodic=True).to(device)
        self.target_name = target_name

        out_c = self.dim_c * 4 if target_name == '*' else self.dim_c
        self.freq_pad = torch.zeros([1, out_c, self.n_bins - self.dim_f, self.dim_t]).to(device)

        self.n = L // 2

    def stft(self, x):
        x = x.reshape([-1, self.chunk_size])
        x = torch.stft(x, n_fft=self.n_fft, hop_length=self.hop, window=self.window, center=True, return_complex=True)
        x = torch.view_as_real(x)
        x = x.permute([0, 3, 1, 2])
        x = x.reshape([-1, 2, 2, self.n_bins, self.dim_t]).reshape([-1, self.dim_c, self.n_bins, self.dim_t])
        return x[:, :, :self.dim_f]

    def istft(self, x, freq_pad=None):
        freq_pad = self.freq_pad.repeat([x.shape[0], 1, 1, 1]) if freq_pad is None else freq_pad
        x = torch.cat([x, freq_pad], -2)
        x = x.reshape([-1, 2, 2, self.n_bins, self.dim_t]).reshape([-1, 2, self.n_bins, self.dim_t])
        x = x.permute([0, 2, 3, 1])
        x = x.contiguous()
        x = torch.view_as_complex(x)
        x = torch.istft(x, n_fft=self.n_fft, hop_length=self.hop, window=self.window, center=True)
        return x.reshape([-1, 2, self.chunk_size])

    def forward(self, x):
        x = self.first_conv(x)
        x = x.transpose(-1, -2)

        ds_outputs = []
        for i in range(self.n):
            x = self.ds_dense[i](x)
            ds_outputs.append(x)
            x = self.ds[i](x)

        x = self.mid_dense(x)
        for i in range(self.n):
            x = self.us[i](x)
            x *= ds_outputs[-i - 1]
            x = self.us_dense[i](x)

        x = x.transpose(-1, -2)
        x = self.final_conv(x)
        return x


def get_models(name, device, load=True, vocals_model_type=0):
    if vocals_model_type == 2:
        model_vocals = Conv_TDF_net_trim_model(
            device=device,
            target_name='vocals',
            L=11,
            n_fft=7680
        )
    elif vocals_model_type == 3:
        model_vocals = Conv_TDF_net_trim_model(
            device=device,
            target_name='vocals',
            L=11,
            n_fft=6144
        )

    return [model_vocals]


def demix_base(mix, device, models, infer_session):
    start_time = time()
    sources = []
    n_sample = mix.shape[1]
    for model in models:
        trim = model.n_fft // 2
        gen_size = model.chunk_size - 2 * trim
        pad = gen_size - n_sample % gen_size
        mix_p = np.concatenate(
            (
                np.zeros((2, trim)),
                mix,
                np.zeros((2, pad)),
                np.zeros((2, trim))
            ), 1
        )

        mix_waves = []
        i = 0
        while i < n_sample + pad:
            waves = np.array(mix_p[:, i:i + model.chunk_size])
            mix_waves.append(waves)
            i += gen_size
        mix_waves = np.array(mix_waves)  # Convert the list to a single numpy.ndarray
        mix_waves = torch.tensor(mix_waves, dtype=torch.float32).to(device)

    with torch.no_grad():
        _ort = infer_session
        stft_res = model.stft(mix_waves)
        res = _ort.run(None, {'input': stft_res.cpu().numpy()})[0]
        ten = torch.tensor(res).to(device)  # Move result tensor to device
        tar_waves = model.istft(ten)  # This operation is performed on the GPU
        tar_waves = tar_waves.cpu()  # Move the result back to CPU only after all computations
        tar_signal = tar_waves[:, :, trim:-trim].transpose(0, 1).reshape(2, -1).numpy()[:, :-pad]

        sources.append(tar_signal)
    # print('Time demix base: {:.2f} sec'.format(time() - start_time))
    return np.array(sources)


def demix_full(mix, device, chunk_size, models, infer_session, overlap=0.75):
    start_time = time()

    step = int(chunk_size * (1 - overlap))
    # print('Initial shape: {} Chunk size: {} Step: {} Device: {}'.format(mix.shape, chunk_size, step, device))
    result = np.zeros((1, 2, mix.shape[-1]), dtype=np.float32)
    divider = np.zeros((1, 2, mix.shape[-1]), dtype=np.float32)

    total = 0
    for i in range(0, mix.shape[-1], step):
        total += 1

        start = i
        end = min(i + chunk_size, mix.shape[-1])
        # print('Chunk: {} Start: {} End: {}'.format(total, start, end))
        mix_part = mix[:, start:end]
        sources = demix_base(mix_part, device, models, infer_session)
        # print(sources.shape)
        result[..., start:end] += sources
        divider[..., start:end] += 1
    sources = result / divider
    # print('Final shape: {} Overall time: {:.2f}'.format(sources.shape, time() - start_time))
    return sources


class EnsembleDemucsMDXMusicSeparationModel:
    def __init__(self, options):
        """
            options - user options
        """
        # print(options)

        if torch.cuda.is_available():
            gpu_id = 0
            if 'gpu_id' in options:
                gpu_id = options['gpu_id']
            device = 'cuda:{}'.format(gpu_id)
        else:
            device = 'cpu'
        if 'cpu' in options:
            if options['cpu']:
                device = 'cpu'
        print('Use device: {}'.format(device))
        self.single_onnx = False
        if 'single_onnx' in options:
            if options['single_onnx']:
                self.single_onnx = True
                print('Use single vocal ONNX')

        self.kim_model_1 = False
        if 'use_kim_model_1' in options:
            if options['use_kim_model_1']:
                self.kim_model_1 = True
        if self.kim_model_1:
            print('Use Kim model 1')
        else:
            print('Use Kim model 2')

        self.overlap_large = float(options['overlap_large'])
        self.overlap_small = float(options['overlap_small'])
        if self.overlap_large > 0.99:
            self.overlap_large = 0.99
        if self.overlap_large < 0.0:
            self.overlap_large = 0.0
        if self.overlap_small > 0.99:
            self.overlap_small = 0.99
        if self.overlap_small < 0.0:
            self.overlap_small = 0.0

        model_folder = os.path.dirname(os.path.realpath(__file__)) + '/models/'
        model_path = model_folder + '04573f0d-f3cf25b2.th'
        model_vocals = load_model(model_path)
        model_vocals.to(device)
        self.model_vocals_only = model_vocals

        self.models = []
        self.weights_vocals = np.array([10, 1, 8, 9])
        self.weights_bass = np.array([19, 4, 5, 8])
        self.weights_drums = np.array([18, 2, 4, 9])
        self.weights_other = np.array([14, 2, 5, 10])

        model1 = pretrained.get_model('htdemucs_ft')
        model1.to(device)
        self.models.append(model1)

        model2 = pretrained.get_model('htdemucs')
        model2.to(device)
        self.models.append(model2)

        model3 = pretrained.get_model('htdemucs_6s')
        model3.to(device)
        self.models.append(model3)

        model4 = pretrained.get_model('hdemucs_mmi')
        model4.to(device)
        self.models.append(model4)

        if 0:
            for model in self.models:
                print(model.sources)
        '''
        ['drums', 'bass', 'other', 'vocals']
        ['drums', 'bass', 'other', 'vocals']
        ['drums', 'bass', 'other', 'vocals', 'guitar', 'piano']
        ['drums', 'bass', 'other', 'vocals']
        '''

        if device == 'cpu':
            chunk_size = 200000000
            providers = ["CPUExecutionProvider"]
        else:
            chunk_size = 1000000
            providers = ["CUDAExecutionProvider"]
        if 'chunk_size' in options:
            chunk_size = int(options['chunk_size'])

        # MDX-B model 1 initialization
        self.chunk_size = chunk_size
        self.mdx_models1 = get_models('tdf_extra', load=False, device=device, vocals_model_type=2)
        model_path_onnx1 = model_folder + 'Kim_Vocal_2.onnx'
        print('Model path: {}'.format(model_path_onnx1))
        print('Device: {} Chunk size: {}'.format(device, chunk_size))
        self.infer_session1 = ort.InferenceSession(
            model_path_onnx1,
            providers=providers,
            provider_options=[{"device_id": 0}],
        )

        if self.single_onnx is False:
            # MDX-B model 2  initialization
            self.chunk_size = chunk_size
            self.mdx_models2 = get_models('tdf_extra', load=False, device=device, vocals_model_type=2)
            root_path = os.path.dirname(os.path.realpath(__file__)) + '/'
            model_path_onnx2 = model_folder + 'Kim_Inst.onnx'
            print('Model path: {}'.format(model_path_onnx2))
            print('Device: {} Chunk size: {}'.format(device, chunk_size))
            self.infer_session2 = ort.InferenceSession(
                model_path_onnx2,
                providers=providers,
                provider_options=[{"device_id": 0}],
            )

        self.device = device
        pass
        
    @property
    def instruments(self):
        """ DO NOT CHANGE """
        return ['bass', 'drums', 'other', 'vocals']

    def raise_aicrowd_error(self, msg):
        """ Will be used by the evaluator to provide logs, DO NOT CHANGE """
        raise NameError(msg)
    
    def separate_music_file(
            self,
            mixed_sound_array,
            sample_rate,
            update_percent_func=None,
            current_file_number=0,
            total_files=0,
            only_vocals=False,
    ):
        """
        Implements the sound separation for a single sound file
        Inputs: Outputs from soundfile.read('mixture.wav')
            mixed_sound_array
            sample_rate

        Outputs:
            separated_music_arrays: Dictionary numpy array of each separated instrument
            output_sample_rates: Dictionary of sample rates separated sequence
        """

        # print('Update percent func: {}'.format(update_percent_func))

        separated_music_arrays = {}
        output_sample_rates = {}

        audio = np.expand_dims(mixed_sound_array.T, axis=0)
        audio = torch.from_numpy(audio).type('torch.FloatTensor').to(self.device)

        overlap_large = self.overlap_large
        overlap_small = self.overlap_small

        # Get Demics vocal only
        model = self.model_vocals_only
        shifts = 1
        overlap = overlap_large
        vocals_demucs = 0.5 * apply_model(model, audio, shifts=shifts, overlap=overlap)[0][3].cpu().numpy()

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.10) / total_files
            update_percent_func(int(val))

        vocals_demucs += 0.5 * -apply_model(model, -audio, shifts=shifts, overlap=overlap)[0][3].cpu().numpy()

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.20) / total_files
            update_percent_func(int(val))

        overlap = overlap_large
        sources1 = demix_full(
            mixed_sound_array.T,
            self.device,
            self.chunk_size,
            self.mdx_models1,
            self.infer_session1,
            overlap=overlap
        )[0]

        vocals_mdxb1 = sources1

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.30) / total_files
            update_percent_func(int(val))

        if self.single_onnx is False:
            sources2 = -demix_full(
                -mixed_sound_array.T,
                self.device,
                self.chunk_size,
                self.mdx_models2,
                self.infer_session2,
                overlap=overlap
            )[0]

            # it's instrumental so need to invert
            instrum_mdxb2 = sources2
            vocals_mdxb2 = mixed_sound_array.T - instrum_mdxb2

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.40) / total_files
            update_percent_func(int(val))

        # Ensemble vocals for MDX and Demucs
        if self.single_onnx is False:
            weights = np.array([12, 8, 3])
            vocals = (weights[0] * vocals_mdxb1.T + weights[1] * vocals_mdxb2.T + weights[2] * vocals_demucs.T) / weights.sum()
        else:
            weights = np.array([6, 1])
            vocals = (weights[0] * vocals_mdxb1.T + weights[1] * vocals_demucs.T) / weights.sum()

        # vocals
        separated_music_arrays['vocals'] = vocals
        output_sample_rates['vocals'] = sample_rate

        if not only_vocals:
            # Generate instrumental
            instrum = mixed_sound_array - vocals

            audio = np.expand_dims(instrum.T, axis=0)
            audio = torch.from_numpy(audio).type('torch.FloatTensor').to(self.device)

            all_outs = []
            for i, model in enumerate(self.models):
                if i == 0:
                    overlap = overlap_small
                elif i > 0:
                    overlap = overlap_large
                out = 0.5 * apply_model(model, audio, shifts=shifts, overlap=overlap)[0].cpu().numpy() \
                      + 0.5 * -apply_model(model, -audio, shifts=shifts, overlap=overlap)[0].cpu().numpy()

                if update_percent_func is not None:
                    val = 100 * (current_file_number + 0.50 + i * 0.10) / total_files
                    update_percent_func(int(val))

                if i == 2:
                    # ['drums', 'bass', 'other', 'vocals', 'guitar', 'piano']
                    out[2] = out[2] + out[4] + out[5]
                    out = out[:4]

                out[0] = self.weights_drums[i] * out[0]
                out[1] = self.weights_bass[i] * out[1]
                out[2] = self.weights_other[i] * out[2]
                out[3] = self.weights_vocals[i] * out[3]

                all_outs.append(out)
            out = np.array(all_outs).sum(axis=0)
            out[0] = out[0] / self.weights_drums.sum()
            out[1] = out[1] / self.weights_bass.sum()
            out[2] = out[2] / self.weights_other.sum()
            out[3] = out[3] / self.weights_vocals.sum()

            # other
            res = mixed_sound_array - vocals - out[0].T - out[1].T
            res = np.clip(res, -1, 1)
            separated_music_arrays['other'] = (2 * res + out[2].T) / 3.0
            output_sample_rates['other'] = sample_rate

            # drums
            res = mixed_sound_array - vocals - out[1].T - out[2].T
            res = np.clip(res, -1, 1)
            separated_music_arrays['drums'] = (res + 2 * out[0].T.copy()) / 3.0
            output_sample_rates['drums'] = sample_rate

            # bass
            res = mixed_sound_array - vocals - out[0].T - out[2].T
            res = np.clip(res, -1, 1)
            separated_music_arrays['bass'] = (res + 2 * out[1].T) / 3.0
            output_sample_rates['bass'] = sample_rate

            bass = separated_music_arrays['bass']
            drums = separated_music_arrays['drums']
            other = separated_music_arrays['other']

            separated_music_arrays['other'] = mixed_sound_array - vocals - bass - drums
            separated_music_arrays['drums'] = mixed_sound_array - vocals - bass - other
            separated_music_arrays['bass'] = mixed_sound_array - vocals - drums - other

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.95) / total_files
            update_percent_func(int(val))

        return separated_music_arrays, output_sample_rates


class EnsembleDemucsMDXMusicSeparationModelLowGPU:
    def __init__(self, options):
        """
            options - user options
        """
        # print(options)

        if torch.cuda.is_available():
            gpu_id = 0
            if 'gpu_id' in options:
                gpu_id = options['gpu_id']
            device = 'cuda:{}'.format(gpu_id)
        else:
            device = 'cpu'
        if 'cpu' in options:
            if options['cpu']:
                device = 'cpu'
        print('Use device: {}'.format(device))
        self.single_onnx = False
        if 'single_onnx' in options:
            if options['single_onnx']:
                self.single_onnx = True
                print('Use single vocal ONNX')

        self.kim_model_1 = False
        if 'use_kim_model_1' in options:
            if options['use_kim_model_1']:
                self.kim_model_1 = True
        if self.kim_model_1:
            print('Use Kim model 1')
        else:
            print('Use Kim model 2')

        self.overlap_large = float(options['overlap_large'])
        self.overlap_small = float(options['overlap_small'])
        if self.overlap_large > 0.99:
            self.overlap_large = 0.99
        if self.overlap_large < 0.0:
            self.overlap_large = 0.0
        if self.overlap_small > 0.99:
            self.overlap_small = 0.99
        if self.overlap_small < 0.0:
            self.overlap_small = 0.0

        self.weights_vocals = np.array([10, 1, 8, 9])
        self.weights_bass = np.array([19, 4, 5, 8])
        self.weights_drums = np.array([18, 2, 4, 9])
        self.weights_other = np.array([14, 2, 5, 10])

        if device == 'cpu':
            chunk_size = 200000000
            self.providers = ["CPUExecutionProvider"]
        else:
            chunk_size = 1000000
            self.providers = ["CUDAExecutionProvider"]
        if 'chunk_size' in options:
            chunk_size = int(options['chunk_size'])
        self.chunk_size = chunk_size
        self.device = device
        pass

    @property
    def instruments(self):
        """ DO NOT CHANGE """
        return ['bass', 'drums', 'other', 'vocals']

    def raise_aicrowd_error(self, msg):
        """ Will be used by the evaluator to provide logs, DO NOT CHANGE """
        raise NameError(msg)

    def separate_music_file(
            self,
            mixed_sound_array,
            sample_rate,
            update_percent_func=None,
            current_file_number=0,
            total_files=0,
            only_vocals=False
    ):
        """
        Implements the sound separation for a single sound file
        Inputs: Outputs from soundfile.read('mixture.wav')
            mixed_sound_array
            sample_rate

        Outputs:
            separated_music_arrays: Dictionary numpy array of each separated instrument
            output_sample_rates: Dictionary of sample rates separated sequence
        """

        # print('Update percent func: {}'.format(update_percent_func))

        separated_music_arrays = {}
        output_sample_rates = {}

        audio = np.expand_dims(mixed_sound_array.T, axis=0)
        audio = torch.from_numpy(audio).type('torch.FloatTensor').to(self.device)

        overlap_large = self.overlap_large
        overlap_small = self.overlap_small

        # Get Demucs vocal only
        model_folder = os.path.dirname(os.path.realpath(__file__)) + '/models/'
        model_path = model_folder + '04573f0d-f3cf25b2.th'
        model_vocals = load_model(model_path)
        model_vocals.to(self.device)
        shifts = 1
        overlap = overlap_large
        vocals_demucs = 0.5 * apply_model(model_vocals, audio, shifts=shifts, overlap=overlap)[0][3].cpu().numpy()

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.10) / total_files
            update_percent_func(int(val))

        vocals_demucs += 0.5 * -apply_model(model_vocals, -audio, shifts=shifts, overlap=overlap)[0][3].cpu().numpy()
        model_vocals = model_vocals.cpu()
        del model_vocals

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.20) / total_files
            update_percent_func(int(val))

        # MDX-B model 1 initialization
        mdx_models1 = get_models('tdf_extra', load=False, device=self.device, vocals_model_type=2)
        model_path_onnx1 = model_folder + 'Kim_Vocal_2.onnx'
        print('Model path: {}'.format(model_path_onnx1))
        print('Device: {} Chunk size: {}'.format(self.device, self.chunk_size))
        infer_session1 = ort.InferenceSession(
            model_path_onnx1,
            providers=self.providers,
            provider_options=[{"device_id": 0}],
        )
        overlap = overlap_large
        sources1 = demix_full(
            mixed_sound_array.T,
            self.device,
            self.chunk_size,
            mdx_models1,
            infer_session1,
            overlap=overlap
        )[0]
        vocals_mdxb1 = sources1
        del infer_session1
        del mdx_models1

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.30) / total_files
            update_percent_func(int(val))

        if self.single_onnx is False:
            # MDX-B model 2  initialization
            mdx_models2 = get_models('tdf_extra', load=False, device=self.device, vocals_model_type=2)
            root_path = os.path.dirname(os.path.realpath(__file__)) + '/'
            model_path_onnx2 = model_folder + 'Kim_Inst.onnx'
            print('Model path: {}'.format(model_path_onnx2))
            print('Device: {} Chunk size: {}'.format(self.device, self.chunk_size))
            infer_session2 = ort.InferenceSession(
                model_path_onnx2,
                providers=self.providers,
                provider_options=[{"device_id": 0}],
            )

            overlap = overlap_large
            sources2 = -demix_full(
                -mixed_sound_array.T,
                self.device,
                self.chunk_size,
                mdx_models2,
                infer_session2,
                overlap=overlap
            )[0]

            # it's instrumental so need to invert
            instrum_mdxb2 = sources2
            vocals_mdxb2 = mixed_sound_array.T - instrum_mdxb2
            del infer_session2
            del mdx_models2

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.40) / total_files
            update_percent_func(int(val))

        # Ensemble vocals for MDX and Demucs
        if self.single_onnx is False:
            weights = np.array([12, 8, 3])
            vocals = (weights[0] * vocals_mdxb1.T + weights[1] * vocals_mdxb2.T + weights[2] * vocals_demucs.T) / weights.sum()
        else:
            weights = np.array([6, 1])
            vocals = (weights[0] * vocals_mdxb1.T + weights[1] * vocals_demucs.T) / weights.sum()

        # Generate instrumental
        instrum = mixed_sound_array - vocals

        audio = np.expand_dims(instrum.T, axis=0)
        audio = torch.from_numpy(audio).type('torch.FloatTensor').to(self.device)

        all_outs = []

        i = 0
        overlap = overlap_small
        model = pretrained.get_model('htdemucs_ft')
        model.to(self.device)
        out = 0.5 * apply_model(model, audio, shifts=shifts, overlap=overlap)[0].cpu().numpy() \
              + 0.5 * -apply_model(model, -audio, shifts=shifts, overlap=overlap)[0].cpu().numpy()

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.50 + i * 0.10) / total_files
            update_percent_func(int(val))

        out[0] = self.weights_drums[i] * out[0]
        out[1] = self.weights_bass[i] * out[1]
        out[2] = self.weights_other[i] * out[2]
        out[3] = self.weights_vocals[i] * out[3]
        all_outs.append(out)
        model = model.cpu()
        del model

        i = 1
        overlap = overlap_large
        model = pretrained.get_model('htdemucs')
        model.to(self.device)
        out = 0.5 * apply_model(model, audio, shifts=shifts, overlap=overlap)[0].cpu().numpy() \
              + 0.5 * -apply_model(model, -audio, shifts=shifts, overlap=overlap)[0].cpu().numpy()

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.50 + i * 0.10) / total_files
            update_percent_func(int(val))

        out[0] = self.weights_drums[i] * out[0]
        out[1] = self.weights_bass[i] * out[1]
        out[2] = self.weights_other[i] * out[2]
        out[3] = self.weights_vocals[i] * out[3]
        all_outs.append(out)
        model = model.cpu()
        del model

        i = 2
        overlap = overlap_large
        model = pretrained.get_model('htdemucs_6s')
        model.to(self.device)
        out = 0.5 * apply_model(model, audio, shifts=shifts, overlap=overlap)[0].cpu().numpy() \
              + 0.5 * -apply_model(model, -audio, shifts=shifts, overlap=overlap)[0].cpu().numpy()

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.50 + i * 0.10) / total_files
            update_percent_func(int(val))

        # More stems need to add
        out[2] = out[2] + out[4] + out[5]
        out = out[:4]
        out[0] = self.weights_drums[i] * out[0]
        out[1] = self.weights_bass[i] * out[1]
        out[2] = self.weights_other[i] * out[2]
        out[3] = self.weights_vocals[i] * out[3]
        all_outs.append(out)
        model = model.cpu()
        del model

        i = 3
        model = pretrained.get_model('hdemucs_mmi')
        model.to(self.device)
        out = 0.5 * apply_model(model, audio, shifts=shifts, overlap=overlap)[0].cpu().numpy() \
              + 0.5 * -apply_model(model, -audio, shifts=shifts, overlap=overlap)[0].cpu().numpy()

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.50 + i * 0.10) / total_files
            update_percent_func(int(val))

        out[0] = self.weights_drums[i] * out[0]
        out[1] = self.weights_bass[i] * out[1]
        out[2] = self.weights_other[i] * out[2]
        out[3] = self.weights_vocals[i] * out[3]
        all_outs.append(out)
        model = model.cpu()
        del model

        out = np.array(all_outs).sum(axis=0)
        out[0] = out[0] / self.weights_drums.sum()
        out[1] = out[1] / self.weights_bass.sum()
        out[2] = out[2] / self.weights_other.sum()
        out[3] = out[3] / self.weights_vocals.sum()

        # vocals
        separated_music_arrays['vocals'] = vocals
        output_sample_rates['vocals'] = sample_rate

        # other
        res = mixed_sound_array - vocals - out[0].T - out[1].T
        res = np.clip(res, -1, 1)
        separated_music_arrays['other'] = (2 * res + out[2].T) / 3.0
        output_sample_rates['other'] = sample_rate

        # drums
        res = mixed_sound_array - vocals - out[1].T - out[2].T
        res = np.clip(res, -1, 1)
        separated_music_arrays['drums'] = (res + 2 * out[0].T.copy()) / 3.0
        output_sample_rates['drums'] = sample_rate

        # bass
        res = mixed_sound_array - vocals - out[0].T - out[2].T
        res = np.clip(res, -1, 1)
        separated_music_arrays['bass'] = (res + 2 * out[1].T) / 3.0
        output_sample_rates['bass'] = sample_rate

        bass = separated_music_arrays['bass']
        drums = separated_music_arrays['drums']
        other = separated_music_arrays['other']

        separated_music_arrays['other'] = mixed_sound_array - vocals - bass - drums
        separated_music_arrays['drums'] = mixed_sound_array - vocals - bass - other
        separated_music_arrays['bass'] = mixed_sound_array - vocals - drums - other

        if update_percent_func is not None:
            val = 100 * (current_file_number + 0.95) / total_files
            update_percent_func(int(val))

        return separated_music_arrays, output_sample_rates


def predict_with_model(options):
    for input_audio in options['input_audio']:
        if not os.path.isfile(input_audio):
            print('Error. No such file: {}. Please check path!'.format(input_audio))
            return
    output_folder = options['output_folder']
    if not os.path.isdir(output_folder):
        os.mkdir(output_folder)

    only_vocals = False
    if 'only_vocals' in options:
        if options['only_vocals'] is True:
            print('Generate only vocals and instrumental')
            only_vocals = True

    model = None
    if 'large_gpu' in options:
        if options['large_gpu'] is True:
            print('Use fast large GPU memory version of code')
            model = EnsembleDemucsMDXMusicSeparationModel(options)
    if model is None:
        print('Use low GPU memory version of code')
        model = EnsembleDemucsMDXMusicSeparationModelLowGPU(options)

    update_percent_func = None
    if 'update_percent_func' in options:
        update_percent_func = options['update_percent_func']

    for i, input_audio in enumerate(options['input_audio']):
        print('Go for: {}'.format(input_audio))
        audio, sr = librosa.load(input_audio, mono=False, sr=44100)
        if len(audio.shape) == 1:
            audio = np.stack([audio, audio], axis=0)
        print("Input audio: {} Sample rate: {}".format(audio.shape, sr))
        result, sample_rates = model.separate_music_file(
            audio.T,
            sr,
            update_percent_func,
            i,
            len(options['input_audio']),
            only_vocals,
        )
        all_instrum = model.instruments
        if only_vocals:
            all_instrum = ['vocals']
        for instrum in all_instrum:
            output_name = os.path.splitext(os.path.basename(input_audio))[0] + '_{}.wav'.format(instrum)
            sf.write(output_folder + '/' + output_name, result[instrum], sample_rates[instrum], subtype='FLOAT')
            print('File created: {}'.format(output_folder + '/' + output_name))

        # instrumental part 1
        inst = audio.T - result['vocals']
        output_name = os.path.splitext(os.path.basename(input_audio))[0] + '_{}.wav'.format('instrum')
        sf.write(output_folder + '/' + output_name, inst, sr, subtype='FLOAT')
        print('File created: {}'.format(output_folder + '/' + output_name))

        if not only_vocals:
            # instrumental part 2
            inst2 = result['bass'] + result['drums'] + result['other']
            output_name = os.path.splitext(os.path.basename(input_audio))[0] + '_{}.wav'.format('instrum2')
            sf.write(output_folder + '/' + output_name, inst2, sr, subtype='FLOAT')
            print('File created: {}'.format(output_folder + '/' + output_name))

    if update_percent_func is not None:
        val = 100
        update_percent_func(int(val))


def md5(fname):
    hash_md5 = hashlib.md5()
    with open(fname, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


if __name__ == '__main__':
    start_time = time()

    print("Version: {}".format(__VERSION__))
    m = argparse.ArgumentParser()
    m.add_argument("--input_audio", "-i", nargs='+', type=str, help="Input audio location. You can provide multiple files at once", required=True)
    m.add_argument("--output_folder", "-r", type=str, help="Output audio folder", required=True)
    m.add_argument("--cpu", action='store_true', help="Choose CPU instead of GPU for processing. Can be very slow.")
    m.add_argument("--overlap_large", "-ol", type=float, help="Overlap of splited audio for light models. Closer to 1.0 - slower", required=False, default=0.6)
    m.add_argument("--overlap_small", "-os", type=float, help="Overlap of splited audio for heavy models. Closer to 1.0 - slower", required=False, default=0.5)
    m.add_argument("--single_onnx", action='store_true', help="Only use single ONNX model for vocals. Can be useful if you have not enough GPU memory.")
    m.add_argument("--chunk_size", "-cz", type=int, help="Chunk size for ONNX models. Set lower to reduce GPU memory consumption. Default: 1000000", required=False, default=1000000)
    m.add_argument("--large_gpu", action='store_true', help="It will store all models on GPU for faster processing of multiple audio files. Requires 11 and more GB of free GPU memory.")
    m.add_argument("--use_kim_model_1", action='store_true', help="Use first version of Kim model (as it was on contest).")
    m.add_argument("--only_vocals", action='store_true', help="Only create vocals and instrumental. Skip bass, drums, other")
    m.add_argument("--gpu_id", "-g", type=int, help="Specify which GPU to use (default: 0)", required=False, default=0)

    gpu_use = "0"
    if '--gpu_id' in sys.argv:
        idx = sys.argv.index('--gpu_id')
        if idx + 1 < len(sys.argv):
            gpu_use = sys.argv[idx + 1]
    elif '-g' in sys.argv:
        idx = sys.argv.index('-g')
        if idx + 1 < len(sys.argv):
            gpu_use = sys.argv[idx + 1]
    
    options = m.parse_args().__dict__
    print("Options: ".format(options))
    for el in options:
        print('{}: {}'.format(el, options[el]))
    predict_with_model(options)
    print('Time: {:.0f} sec'.format(time() - start_time))
    print('Presented by https://mvsep.com')


"""
Example:
    python inference.py
    --input_audio mixture.wav mixture1.wav
    --output_folder ./results/
    --cpu
    --overlap_large 0.25
    --overlap_small 0.25
    --chunk_size 500000
"""
