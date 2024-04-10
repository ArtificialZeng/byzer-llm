import asyncio
import json
import threading
import time
import traceback
from typing import List, Dict, Any, Union
import ray
from anthropic import Anthropic

from byzerllm.utils import BlockVLLMStreamServer, StreamOutputs, SingleOutput, SingleOutputMeta


class CustomSaasAPI:
    def __init__(self, infer_params: Dict[str, str]) -> None:
        self.api_key: str = infer_params["saas.api_key"]
        self.model = infer_params.get("saas.model", "claude-3-haiku-20240307")
        self.meta = {
            "model_deploy_type": "saas",
            "backend": "saas",
            "support_stream": True
        }

        self.client = Anthropic(api_key=self.api_key)

        try:
            ray.get_actor("BLOCK_VLLM_STREAM_SERVER")
        except ValueError:
            ray.remote(BlockVLLMStreamServer).options(name="BLOCK_VLLM_STREAM_SERVER", lifetime="detached",
                                                      max_concurrency=1000).remote()

    # saas/proprietary
    def get_meta(self):
        return [self.meta]

    async def async_stream_chat(
            self,
            tokenizer,
            ins: str,
            his: List[dict] = [],
            max_length: int = 1024,
            top_p: float = 0.9,
            temperature: float = 0.1,
            **kwargs
    ):
        messages = []
        system_message = ""
        for message in his:
            if message["role"] == "system":
                system_message = message["content"]
            else:
                messages.append({"role": message["role"], "content": message["content"]})

        messages.append({"role": "user", "content": ins})

        start_time = time.monotonic()

        other_params = {}

        if system_message:
            other_params["system"] = system_message
        
        if "stream" in kwargs:
            other_params["stream"] = kwargs["stream"]        

        stream = kwargs.get("stream", False)

        try:
            res_data = await self.client.messages.create(                
                model=self.model,
                max_tokens=max_length,
                temperature=temperature,
                top_p=top_p,
                messages=messages,                
                **other_params
            )
        except Exception as e:
            traceback.print_exc()
            raise e
                

        if stream:
            server = ray.get_actor("BLOCK_VLLM_STREAM_SERVER")
            request_id = [None]

            async def writer():
                input_tokens = 0
                async for response in res_data:                     

                    if response.type == "message_start":     
                        request_id[0] = response.message.id
                        input_tokens = response.message.usage.input_tokens

                    if response.type == "content_block_delta":    
                        v = response.delta.text                    
                        await server.add_item.remote(request_id[0],
                                                    StreamOutputs(outputs=[SingleOutput(text=v, metadata=SingleOutputMeta(
                                                        input_tokens_count=0,
                                                        generated_tokens_count=0,
                                                    ))])
                                                    )
                    if response.type == "message_delta":
                        await server.add_item.remote(request_id[0],
                                                    StreamOutputs(outputs=[SingleOutput(text="", metadata=SingleOutputMeta(
                                                        input_tokens_count=input_tokens,
                                                        generated_tokens_count=response.usage.output_tokens,
                                                    ))])
                                                    )


                await server.mark_done.remote(request_id[0])

            threading.Thread(target=asyncio.run, args=(writer(),), daemon=True).start()

            time_count = 10 * 100
            while request_id[0] is None and time_count > 0:
                time.sleep(0.01)
                time_count -= 1

            if request_id[0] is None:
                raise Exception("Failed to get request id")

            def write_running():
                return ray.get(server.add_item.remote(request_id[0], "RUNNING"))

            await asyncio.to_thread(write_running)
            return [("", {"metadata": {"request_id": request_id[0], "stream_server": "BLOCK_VLLM_STREAM_SERVER"}})]

        time_cost = time.monotonic() - start_time

        generated_text = res_data.content[0].text
        generated_tokens_count = res_data.usage.output_tokens
        input_tokens_count = res_data.usage.input_tokens

        return [(generated_text, {"metadata": {
            "request_id": res_data.id,
            "input_tokens_count": input_tokens_count,
            "generated_tokens_count": generated_tokens_count,
            "time_cost": time_cost,
            "first_token_time": 0,
            "speed": float(generated_tokens_count) / time_cost,
            "stop_reason": res_data.stop_reason
        }})]