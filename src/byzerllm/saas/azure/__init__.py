import time
from typing import List, Tuple, Dict, Any, Union
import requests
import base64
import io
import json
import ray
from byzerllm.utils.types import BlockVLLMStreamServer, StreamOutputs, SingleOutput, SingleOutputMeta, BlockBinaryStreamServer
import threading
import asyncio
import traceback
import uuid
import os

class CustomSaasAPI:

    def __init__(self, infer_params: Dict[str, str]) -> None:
        self.subscription_key = infer_params["saas.subscription_key"]
        self.region = infer_params["saas.region"]
        self.voice_name = infer_params.get("saas.voice_name", "en-US-AvaMultilingualNeural")

        self.base_url = f"https://{self.region}.tts.speech.microsoft.com"

        self.max_retries = int(infer_params.get("saas.max_retries", 10))

        self.meta = {
            "model_deploy_type": "saas",
            "backend": "saas",
            "support_stream": True,
            "model_name": "azure_tts",
        }

        try:
            ray.get_actor("BLOCK_VLLM_STREAM_SERVER")
        except ValueError:
            try:
                ray.remote(BlockVLLMStreamServer).options(name="BLOCK_VLLM_STREAM_SERVER", lifetime="detached", max_concurrency=1000).remote()
            except Exception as e:
                pass
        try:
            ray.get_actor("BlockBinaryStreamServer")
        except ValueError:
            try:
                ray.remote(BlockBinaryStreamServer).options(name="BlockBinaryStreamServer", lifetime="detached", max_concurrency=1000).remote()
            except Exception as e:
                pass

    def get_meta(self):
        return [self.meta]

    def process_input(self, ins: Union[str, List[Dict[str, Any]], Dict[str, Any]]):
        if isinstance(ins, list) or isinstance(ins, dict):
            return ins

        content = []
        try:
            ins_json = json.loads(ins)
        except:
            return ins

        if isinstance(ins_json, dict):
            return ins_json

        content = []
        for item in ins_json:
            if "image" in item or "image_url" in item:
                image_data = item.get("image", item.get("image_url", ""))
                if not image_data.startswith("data:"):
                    image_data = "data:image/jpeg;base64," + image_data
                content.append({"image_url": {"url": image_data}, "type": "image_url",})
            elif "text" in item:
                text_data = item["text"]
                content.append({"text": text_data, "type": "text"})
        if not content:
            return ins

        return content 

    async def text_to_speech(self, stream: bool, ins: str, voice: str, chunk_size: int = None, response_format: str = "mp3", **kwargs):
        request_id = [None]
        request_json = {
            "voice_name": voice,
            "text": ins,
            "response_format": response_format  
        }

        header = {
            "Ocp-Apim-Subscription-Key": self.subscription_key,
            "Content-Type": "application/json",
            "X-Microsoft-OutputFormat": response_format
        }

        if stream:
            server = ray.get_actor("BlockBinaryStreamServer")

            def writer():
                request_id[0] = str(uuid.uuid4())
                try:
                    response = requests.post(f"{self.base_url}/cognitiveservices/v1", json=request_json, headers=header, stream=True)
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            ray.get(server.add_item.remote(request_id[0],
                                                           StreamOutputs(outputs=[SingleOutput(text=chunk, metadata=SingleOutputMeta(
                                                               input_tokens_count=0,
                                                               generated_tokens_count=0,
                                                           ))])
                                                           ))
                except:
                    traceback.print_exc()
                ray.get(server.mark_done.remote(request_id[0]))

            threading.Thread(target=writer, daemon=True).start()

            time_count = 10 * 100
            while request_id[0] is None and time_count > 0:
                time.sleep(0.01)
                time_count -= 1

            if request_id[0] is None:
                raise Exception("Failed to get request id")

            def write_running():
                return ray.get(server.add_item.remote(request_id[0], "RUNNING"))

            await asyncio.to_thread(write_running)
            return [("", {"metadata": {"request_id": request_id[0], "stream_server": "BlockBinaryStreamServer"}})]

        start_time = time.monotonic()
        request_id[0] = str(uuid.uuid4())
        response = requests.post(f"{self.base_url}/cognitiveservices/v1", json=request_json, headers=header)
        if response.status_code == 200:
            data = response.content
        else:
            raise Exception(f"Failed to get response: {response.text}")
        time_cost = time.monotonic() - start_time
        return [(data, {"metadata": {
            "request_id": "",
            "input_tokens_count": 0,
            "generated_tokens_count": 0,
            "time_cost": time_cost,
            "first_token_time": 0,
            "speed": 0,
        }})]

    def speech_to_text(self, ins: str, **kwargs):
        return None

    def image_to_text(self, ins: str, **kwargs):
        return None

    def text_to_image(self, ins: str, **kwargs):
        return None

    def text_to_text(self, ins: str, **kwargs):
        return None

    async def async_stream_chat(self, tokenizer, ins: str, his: List[Dict[str, Any]] = [],
                                max_length: int = 4096,
                                top_p: float = 0.7,
                                temperature: float = 0.9, **kwargs):

        stream = kwargs.get("stream", False)

        messages = [{"role": message["role"], "content": self.process_input(message["content"])} for message in
                    his] + [{"role": "user", "content": self.process_input(ins)}]
        last_message = messages[-1]["content"]

        if isinstance(last_message, dict) and "input" in last_message:
            voice = last_message.get("voice", self.voice_name)
            chunk_size = last_message.get("chunk_size", None)
            return await self.text_to_speech(stream=stream,
                                             ins=ins,
                                             voice=voice,
                                             chunk_size=chunk_size)

        raise Exception("Invalid input")