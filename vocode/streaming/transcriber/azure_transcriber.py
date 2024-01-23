import logging
import queue
from typing import Optional
import json

import azure.cognitiveservices.speech as speechsdk
from azure.cognitiveservices.speech import SpeechRecognitionResult
from azure.cognitiveservices.speech.audio import (
    PushAudioInputStream,
    AudioStreamFormat,
    AudioStreamWaveFormat,
)

from vocode import getenv

from vocode.streaming.models.audio_encoding import AudioEncoding
from vocode.streaming.transcriber.base_transcriber import (
    BaseThreadAsyncTranscriber,
    Transcription,
)
from vocode.streaming.models.transcriber import AzureTranscriberConfig


class AzureTranscriber(BaseThreadAsyncTranscriber[AzureTranscriberConfig]):
    def __init__(
        self,
        transcriber_config: AzureTranscriberConfig,
        logger: Optional[logging.Logger] = None,
    ):
        super().__init__(transcriber_config)
        self.logger = logger
        self.audio_cursor = 0.0

        format = None
        if self.transcriber_config.audio_encoding == AudioEncoding.LINEAR16:
            format = AudioStreamFormat(
                samples_per_second=self.transcriber_config.sampling_rate,
                wave_stream_format=AudioStreamWaveFormat.PCM,
            )

        elif self.transcriber_config.audio_encoding == AudioEncoding.MULAW:
            format = AudioStreamFormat(
                samples_per_second=self.transcriber_config.sampling_rate,
                wave_stream_format=AudioStreamWaveFormat.MULAW,
            )

        self.push_stream = PushAudioInputStream(format)

        config = speechsdk.audio.AudioConfig(stream=self.push_stream)

        speech_config = speechsdk.SpeechConfig(
            subscription=getenv("AZURE_SPEECH_KEY"),
            region=getenv("AZURE_SPEECH_REGION"),
        )
        # set detailed output format to get confidence
        speech_config.output_format = speechsdk.OutputFormat.Detailed
        speech_params = {
            "speech_config": speech_config,
            "audio_config": config,
        }

        if self.transcriber_config.azure_endpointing:
            speech_config.set_property(
                property_id=speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
                value=str(self.transcriber_config.azure_endpointing),
            )

        if self.transcriber_config.candidate_languages:
            speech_config.set_property(
                property_id=speechsdk.PropertyId.SpeechServiceConnection_LanguageIdMode,
                value="Continuous",
            )
            auto_detect_source_language_config = (
                speechsdk.languageconfig.AutoDetectSourceLanguageConfig(
                    languages=self.transcriber_config.candidate_languages
                )
            )

            speech_params[
                "auto_detect_source_language_config"
            ] = auto_detect_source_language_config
        else:
            speech_params["language"] = self.transcriber_config.language
        logger.debug(f"Azure params: {speech_params}")
        self.speech = speechsdk.SpeechRecognizer(**speech_params)

        self._ended = False
        self.is_ready = False

    def calculate_time_silent(self, json_result: dict) -> float:
        ticks_in_second = 1e7
        end  = json_result["Offset"] + json_result["Duration"]
        words = json_result["NBest"][0]["Words"]
        if words:
            last_word_end = words[-1]["Offset"] + words[-1]["Duration"]
            return (end - last_word_end)/ticks_in_second
        return json_result["Duration"]/ticks_in_second


    def recognized_sentence_final(self, evt):
        result: SpeechRecognitionResult = evt.result
        json_result = json.loads(result.json)
        message = json_result["DisplayText"]
        confidence = json_result["NBest"][0]["Confidence"]
        duration = json_result["Duration"]
        ms_in_second = 1000
        latency = int(result.properties[
            speechsdk.PropertyId.SpeechServiceResponse_RecognitionLatencyMs
        ])/ms_in_second
        time_silent = self.calculate_time_silent(json_result)
        self.output_janus_queue.sync_q.put_nowait(
            Transcription(
                message=message,
                confidence=confidence,
                is_final=True,
                latency=latency,
                time_silent=time_silent,
                duration=duration,
            )
        )

    def recognized_sentence_stream(self, evt):
        result: SpeechRecognitionResult = evt.result
        json_result = json.loads(result.json)
        message = json_result["Text"]
        duration = json_result["Duration"]
        ms_in_second = 1000
        latency = int(result.properties[
            speechsdk.PropertyId.SpeechServiceResponse_RecognitionLatencyMs
        ])/ms_in_second
        self.output_janus_queue.sync_q.put_nowait(
            Transcription(
                message=message,
                confidence=1.0,
                is_final=False,
                latency=latency,
                duration=duration,
            )
        )

    def _run_loop(self):
        stream = self.generator()
        self.logger.debug("Got Azure generator stream")
        def stop_cb(evt):
            self.logger.debug("CLOSING on {}".format(evt))
            self.speech.stop_continuous_recognition()
            self._ended = True

        self.speech.recognizing.connect(lambda x: self.recognized_sentence_stream(x))
        self.speech.recognized.connect(lambda x: self.recognized_sentence_final(x))
        self.speech.session_started.connect(
            lambda evt: self.logger.debug("SESSION STARTED: {}".format(evt))
        )
        self.speech.session_stopped.connect(
            lambda evt: self.logger.debug("SESSION STOPPED {}".format(evt))
        )
        self.speech.canceled.connect(
            lambda evt: self.logger.debug("CANCELED {}".format(evt))
        )

        self.speech.session_stopped.connect(stop_cb)
        self.speech.canceled.connect(stop_cb)
        self.speech.start_continuous_recognition_async()

        for content in stream:
            self.push_stream.write(content)
            if self._ended:
                break

    def generator(self):
        while not self._ended:
            # Use a blocking get() to ensure there's at least one chunk of
            # data, and stop iteration if the chunk is None, indicating the
            # end of the audio stream.
            try:
                chunk = self.input_janus_queue.sync_q.get(timeout=5)
            except queue.Empty:
                return

            if chunk is None:
                return
            data = [chunk]

            # Now consume whatever other data's still buffered.
            while True:
                try:
                    chunk = self.input_janus_queue.sync_q.get_nowait()
                    if chunk is None:
                        return
                    data.append(chunk)
                except queue.Empty:
                    break
                num_channels = 1
                sample_width = 2
                self.audio_cursor += len(chunk) / (
                    self.transcriber_config.sampling_rate * num_channels * sample_width
                )

            yield b"".join(data)

    async def terminate(self):
        self._ended = True
        self.speech.stop_continuous_recognition_async()
        super().terminate()
