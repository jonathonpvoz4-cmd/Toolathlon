# -*- coding: utf-8 -*-
import json
import asyncio


from utils.aux_tools.web_search import on_web_search_tool_invoke


asyncio.run(on_web_search_tool_invoke(None, '{"query": "China", "num_results": 10}'))