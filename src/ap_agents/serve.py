"""Start agents as A2A servers. `python -m ap_agents.serve` starts all of them; pass names to start a subset."""

import asyncio
import sys

import uvicorn

from ap_agents.agent import HOST, Agent
from ap_agents.config import load_config


async def main(names: list[str]) -> None:
    cfg = load_config()
    agents = [Agent(n, cfg) for n in names or cfg["agents"]]
    for a in agents:
        await a.start()
        print(f"{a.name} on port {a.spec['port']} with tools {[t.name for t in a.tools]}", flush=True)
    servers = [uvicorn.Server(uvicorn.Config(a.app(), host=HOST, port=a.spec["port"], log_level="warning"))
               for a in agents]
    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        for a in agents:
            await a.stop()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
