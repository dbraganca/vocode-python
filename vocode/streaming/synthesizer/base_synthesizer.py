import asyncio
import os
import __main__
from typing import (
    Any,
    Dict,
    AsyncGenerator,
    Generator,
    Callable,
    Generic,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union
)
import math
import io
import wave
import aiohttp
from  aiobotocore.session import get_session
from nltk.tokenize import word_tokenize
from nltk.tokenize.treebank import TreebankWordDetokenizer
from opentelemetry import trace
from opentelemetry.trace import Span

from vocode.streaming.utils.cache import RedisRenewableTTLCache
from vocode.streaming.agent.bot_sentiment_analyser import BotSentiment
from vocode.streaming.models.agent import (
    FillerAudioConfig, 
    FollowUpAudioConfig,
    BacktrackAudioConfig
)
from vocode.streaming.models.message import BaseMessage
from vocode.streaming.synthesizer.miniaudio_worker import MiniaudioWorker
from vocode.streaming.utils import convert_wav, get_chunk_size_per_second
from vocode.streaming.models.audio_encoding import AudioEncoding
from vocode.streaming.models.synthesizer import SynthesizerConfig, TYPING_NOISE_PATH
import logging
import base64

# Get the root logger
logger = logging.getLogger()

def encode_as_wav(chunk: bytes, synthesizer_config: SynthesizerConfig) -> bytes:
    output_bytes_io = io.BytesIO()
    in_memory_wav = wave.open(output_bytes_io, "wb")
    in_memory_wav.setnchannels(1)
    assert synthesizer_config.audio_encoding == AudioEncoding.LINEAR16
    in_memory_wav.setsampwidth(2)
    in_memory_wav.setframerate(synthesizer_config.sampling_rate)
    in_memory_wav.writeframes(chunk)
    output_bytes_io.seek(0)
    return output_bytes_io.read()


tracer = trace.get_tracer(__name__)


class SynthesisResult:
    class ChunkResult:
        def __init__(self, chunk: bytes, is_last_chunk: bool):
            self.chunk = chunk
            self.is_last_chunk = is_last_chunk

    def __init__(
        self,
        chunk_generator: AsyncGenerator[ChunkResult, None],
        get_message_up_to: Callable[[float], str],
    ):
        self.chunk_generator = chunk_generator
        self.get_message_up_to = get_message_up_to


class FillerAudio:
    def __init__(
        self,
        message: BaseMessage,
        audio_data: bytes,
        synthesizer_config: SynthesizerConfig,
        is_interruptible: bool = False,
        seconds_per_chunk: int = 1,
    ):
        self.message = message
        self.audio_data = audio_data
        self.synthesizer_config = synthesizer_config
        self.is_interruptible = is_interruptible
        self.seconds_per_chunk = seconds_per_chunk

    def create_synthesis_result(self) -> SynthesisResult:
        chunk_size = (
            get_chunk_size_per_second(
                self.synthesizer_config.audio_encoding,
                self.synthesizer_config.sampling_rate,
            )
            * self.seconds_per_chunk
        )

        async def chunk_generator(chunk_transform=lambda x: x):
            for i in range(0, len(self.audio_data), chunk_size):
                if i + chunk_size > len(self.audio_data):
                    yield SynthesisResult.ChunkResult(
                        chunk_transform(self.audio_data[i:]), True
                    )
                else:
                    yield SynthesisResult.ChunkResult(
                        chunk_transform(self.audio_data[i : i + chunk_size]), False
                    )

        if self.synthesizer_config.should_encode_as_wav:
            output_generator = chunk_generator(
                lambda chunk: encode_as_wav(chunk, self.synthesizer_config)
            )
        else:
            output_generator = chunk_generator()
        return SynthesisResult(output_generator, lambda seconds: self.message.text)


SynthesizerConfigType = TypeVar("SynthesizerConfigType", bound=SynthesizerConfig)


class BaseSynthesizer(Generic[SynthesizerConfigType]):
    def __init__(
        self,
        synthesizer_config: SynthesizerConfigType,
        cache: Optional[RedisRenewableTTLCache] = None,
        logger: Optional[logging.Logger] = None,
        aiohttp_session: Optional[aiohttp.ClientSession] = None,
    ):
        self.logger = logger or logging.getLogger(__name__)
        self.cache: RedisRenewableTTLCache = cache
        self.synthesizer_config = synthesizer_config

        if synthesizer_config.audio_encoding == AudioEncoding.MULAW:
            assert (
                synthesizer_config.sampling_rate == 8000
            ), "MuLaw encoding only supports 8kHz sampling rate"

        self.base_filler_audio_path = self.synthesizer_config.base_filler_audio_path
        self.base_follow_up_audio_path = self.synthesizer_config.base_follow_up_audio_path
        self.base_backtrack_audio_path = self.synthesizer_config.base_backtrack_audio_path
        
        self.filler_audios: Dict[str,List[FillerAudio]] = {}
        self.follow_up_audios: List[FillerAudio] = []
        self.backtrack_audios: List[FillerAudio] = []

        if aiohttp_session:
            # the caller is responsible for closing the session
            self.aiohttp_session = aiohttp_session
            self.should_close_session_on_tear_down = False
        else:
            self.aiohttp_session = aiohttp.ClientSession()
            self.should_close_session_on_tear_down = True
        
        self.aiobotocore_session = get_session()

    async def empty_generator(self):
        yield SynthesisResult.ChunkResult(b"", True)

    def get_synthesizer_config(self) -> SynthesizerConfig:
        return self.synthesizer_config

    def get_typing_noise_filler_audio(self) -> FillerAudio:
        return FillerAudio(
            message=BaseMessage(text="<typing noise>"),
            audio_data=convert_wav(
                TYPING_NOISE_PATH,
                output_sample_rate=self.synthesizer_config.sampling_rate,
                output_encoding=self.synthesizer_config.audio_encoding,
            ),
            synthesizer_config=self.synthesizer_config,
            is_interruptible=True,
            seconds_per_chunk=2,
        )

    def make_filler_phrase_list(
            self, filler_dict: Dict[str, List[str]]
    ) -> List[BaseMessage]:
        filler_list = [] 
        for key in filler_dict:
            filler_list += filler_dict[key]
        filler_list = list(set(filler_list))

        filler_phrase_list = [
            BaseMessage(text=filler) for filler in filler_list
        ]
        return filler_phrase_list


    async def set_filler_audios(self, filler_audio_config: FillerAudioConfig):
        self.logger.debug(f"Setting filler audios...")
        if filler_audio_config.use_phrases:
            self.filler_audios = await self.get_phrase_filler_audios(filler_audio_config)
        elif filler_audio_config.use_typing_noise:
            self.filler_audios = {"typing": [self.get_typing_noise_filler_audio()]}

    async def set_follow_up_audios(self, follow_up_audio_config: FollowUpAudioConfig):
        self.logger.debug(f"Setting follow up audios...")
        if follow_up_audio_config:
            if follow_up_audio_config.follow_up_phrases:
                self.follow_up_audios = await self.get_phrase_follow_up_audios(
                    follow_up_audio_config
                )
            else:
                self.follow_up_audios = await self.get_phrase_follow_up_audios()
    
    async def set_backtrack_audios(self, backtrack_audio_config: BacktrackAudioConfig):
        self.logger.debug(f"Setting backtrack audios...")
        if backtrack_audio_config:
            if backtrack_audio_config.backtrack_phrases:
                self.backtrack_audios = await self.get_phrase_backtrack_audios(
                    backtrack_audio_config
                )
            else:
                self.backtrack_audios = await self.get_phrase_backtrack_audios()
        


    async def get_phrase_filler_audios(self, filler_audio_config) -> Dict[str, List[FillerAudio]]:
        return []
    
    async def get_phrase_follow_up_audios(
        self, follow_up_audio_config: FollowUpAudioConfig
    ) -> List[FillerAudio]:
        language = follow_up_audio_config.language
        follow_up_phrases = follow_up_audio_config.follow_up_phrases.get(language)
        self.logger.debug(f"Generating follow up audios: {follow_up_phrases}")
        follow_up_audios = await self.get_audios_from_messages(
            follow_up_phrases, self.base_follow_up_audio_path
        )
        return follow_up_audios
    
    async def get_phrase_backtrack_audios(
        self, backtrack_audio_config: BacktrackAudioConfig
    ) -> List[FillerAudio]:
        language = backtrack_audio_config.language
        backtrack_phrases = backtrack_audio_config.backtrack_phrases.get(language)
        self.logger.debug(f"Generating backtrack audios for {backtrack_phrases}")
        backtrack_audios = await self.get_audios_from_messages(
            backtrack_phrases, self.base_backtrack_audio_path
        )
        return backtrack_audios

    async def get_audios_from_messages(
            self, 
            phrases: List[BaseMessage],
            base_path: str,
            audio_is_interruptible: bool = True,
    ) -> List[FillerAudio]:
        return []

    def ready_synthesizer(self):
        pass

    # given the number of seconds the message was allowed to go until, where did we get in the message?
    @staticmethod
    def get_message_cutoff_from_total_response_length(
        synthesizer_config: SynthesizerConfig,
        message: BaseMessage,
        seconds: float,
        size_of_output: int,
    ) -> str:
        estimated_output_seconds = size_of_output / synthesizer_config.sampling_rate
        if not message.text:
            return message.text

        estimated_output_seconds_per_char = estimated_output_seconds / len(message.text)
        return message.text[: int(seconds / estimated_output_seconds_per_char)]

    @staticmethod
    def get_message_cutoff_from_voice_speed(
        message: BaseMessage, seconds: float, words_per_minute: int
    ) -> str:
        words_per_second = words_per_minute / 60
        estimated_words_spoken = math.floor(words_per_second * seconds)
        tokens = word_tokenize(message.text)
        return TreebankWordDetokenizer().detokenize(tokens[:estimated_words_spoken])

    async def create_speech(
        self,
        message: BaseMessage,
        chunk_size: int,
        bot_sentiment: Optional[BotSentiment] = None,
        conversation_id: Optional[str] = None
    ) -> SynthesisResult:
        '''
        returns a chunk generator and a thunk that can tell you what part of the message was read given the number of seconds spoken
        chunk generator must return a ChunkResult, essentially a tuple (bytes of size chunk_size, flag if it is the last chunk)
        '''
        raise NotImplementedError

    # @param file - a file-like object in wav format
    @staticmethod
    def create_synthesis_result_from_wav(
        synthesizer_config: SynthesizerConfig,
        file: Any,
        message: BaseMessage,
        chunk_size: int,
    ) -> SynthesisResult:
        output_bytes = convert_wav(
            file,
            output_sample_rate=synthesizer_config.sampling_rate,
            output_encoding=synthesizer_config.audio_encoding,
        )

        if synthesizer_config.should_encode_as_wav:
            chunk_transform = lambda chunk: encode_as_wav(chunk, synthesizer_config)
        else:
            chunk_transform = lambda chunk: chunk

        async def chunk_generator(output_bytes):
            for i in range(0, len(output_bytes), chunk_size):
                if i + chunk_size > len(output_bytes):
                    yield SynthesisResult.ChunkResult(
                        chunk_transform(output_bytes[i:]), True
                    )
                else:
                    yield SynthesisResult.ChunkResult(
                        chunk_transform(output_bytes[i : i + chunk_size]), False
                    )

        return SynthesisResult(
            chunk_generator(output_bytes),
            lambda seconds: BaseSynthesizer.get_message_cutoff_from_total_response_length(
                synthesizer_config, message, seconds, len(output_bytes)
            ),
        )

    async def experimental_mp3_streaming_output_generator(
        self,
        response: aiohttp.ClientResponse,
        chunk_size: int,
        create_speech_span: Optional[Span],
        message: Optional[BaseMessage] = None
    ) -> AsyncGenerator[SynthesisResult.ChunkResult, None]:
        miniaudio_worker_input_queue: asyncio.Queue[
            Union[bytes, None]
        ] = asyncio.Queue()
        miniaudio_worker_output_queue: asyncio.Queue[
            Tuple[bytes, bool]
        ] = asyncio.Queue()
        miniaudio_worker = MiniaudioWorker(
            self.synthesizer_config,
            chunk_size,
            miniaudio_worker_input_queue,
            miniaudio_worker_output_queue,
            logger=self.logger
        )
        miniaudio_worker.start()
        stream_reader = response.content

        # Create a task to send the mp3 chunks to the MiniaudioWorker's input queue in a separate loop
        async def send_chunks():
            got_first = False
            mp3_audio = b""
            try:
                async for chunk in stream_reader.iter_any():
                    miniaudio_worker.consume_nonblocking(chunk)
                    mp3_audio += chunk
                    if not got_first:
                        got_first = True
                        if create_speech_span is not None:
                            create_speech_span.end()
            except asyncio.TimeoutError:
                self.logger.debug("Timeout while reading chunks from stream_reader")
            finally:
                miniaudio_worker.consume_nonblocking(None)  # sentinel
                cache_key = self.get_cache_key(message.text)
                self.logger.debug(f"Caching {cache_key} in experimental_mp3_streaming")
                self.cache.set(cache_key, base64.b64encode(mp3_audio))

        try:
            asyncio.create_task(send_chunks())
            # wav_audio = b""

            # Await the output queue of the MiniaudioWorker and yield the wav chunks in another loop
            while True:
                # Get the wav chunk and the flag from the output queue of the MiniaudioWorker
                wav_chunk, is_last = await miniaudio_worker.output_queue.get()
                if self.synthesizer_config.should_encode_as_wav:
                    wav_chunk = encode_as_wav(wav_chunk, self.synthesizer_config)
                # wav_audio += wav_chunk
                yield SynthesisResult.ChunkResult(wav_chunk, is_last)
                # If this is the last chunk, break the loop
                if is_last:
                    # cache_key = self.get_cache_key(message.text)
                    # self.logger.debug(f"Caching {cache_key}")
                    # self.cache.set(cache_key, wav_audio)
                    self.logger.debug("Last chunk in MiniaudioWorker")
                    break
        except asyncio.TimeoutError:
            self.logger.debug("MiniaudioWorker timed out")
            pass
        except asyncio.CancelledError:
            pass
        finally:
            miniaudio_worker.terminate()

    async def tear_down(self):
        if self.should_close_session_on_tear_down:
            await self.aiohttp_session.close()

    def get_cache_key(self, text: str) -> str:
        return self.synthesizer_config.get_cache_key(text)
