import os
import csv
import asyncio
import time
import logging
import yaml
import re
from pathlib import Path
from typing import List, Dict
from collections import defaultdict

# Add the root directory to Python path
import sys
root_dir = Path(__file__).parent.parent
sys.path.insert(0, str(root_dir))

from mcp_servers.multiMCP import MultiMCP
from agent.agent_loop2 import AgentLoop
from memory.session_log import extract_session_state

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
NUM_QUERIES = 105
SLEEP_TIME = 5
INPUT_FILE = "/home/shiv-nlp-mldl-cv/Documents/EAGCode/Session10-Multi-Agent_Systems_and_Distributed_AI_Coordination/MCP-MAS-main/performance/test_queries_input.csv"
OUTPUT_FILE = "/home/shiv-nlp-mldl-cv/Documents/EAGCode/Session10-Multi-Agent_Systems_and_Distributed_AI_Coordination/MCP-MAS-main/performance/test_queries_output.csv"

class ToolStats:
    def __init__(self):
        self.total_calls = 0
        self.success_calls = 0
        self.error_calls = 0

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return (self.success_calls / self.total_calls) * 100

class QueryTester:
    def __init__(self, multi_mcp, agent_loop):
        self.multi_mcp = multi_mcp
        self.agent_loop = agent_loop
        self.tool_stats = defaultdict(ToolStats)

    @classmethod
    async def create(cls):
        # Load server configs from yaml
        with open(root_dir / "config" / "mcp_server_config.yaml", "r") as f:
            server_configs = yaml.safe_load(f)

        with open("config/mcp_server_config.yaml", "r") as f:
            profile = yaml.safe_load(f)
            mcp_servers_list = profile.get("mcp_servers", [])
            configs = list(mcp_servers_list)

        multi_mcp = MultiMCP(server_configs=configs)
        await multi_mcp.initialize()

        agent_loop = AgentLoop(
            perception_prompt_path="prompts/perception_prompt.txt",
            decision_prompt_path="prompts/decision_prompt.txt",
            multi_mcp=multi_mcp,
            strategy="exploratory"
        )

        return cls(multi_mcp, agent_loop)

    def log_tool_stats(self, tool_usage: List[Dict]) -> None:
        for tool in tool_usage:
            tool_name = tool["tool_name"]
            status = tool["status"]
            self.tool_stats[tool_name].total_calls += 1
            if status == "success":
                self.tool_stats[tool_name].success_calls += 1
            else:
                self.tool_stats[tool_name].error_calls += 1

    def write_tool_performance_report(self) -> None:
        performance_dir = "/home/shiv-nlp-mldl-cv/Documents/EAGCode/Session10-Multi-Agent_Systems_and_Distributed_AI_Coordination/MCP-MAS-main/performance"
        
        output_file = os.path.join(performance_dir,"tool_performance_status.csv")
        with open(output_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Tool Name", "Number of Times Invoked", "Success Rate (%)"])
            for tool_name, stats in sorted(self.tool_stats.items()):
                writer.writerow([
                    tool_name,
                    stats.total_calls,
                    f"{stats.success_rate:.2f}"
                ])
        print(f"✅ Tool performance report written to {output_file}")

    async def execute_query(self, query: str, tools: str, complexity: str) -> Dict:
        try:
            session = await self.agent_loop.run(query)
            session_state = extract_session_state(session)
            final_plan = session_state["final_plan"]
            if isinstance(final_plan, list):
                final_plan = "\n".join(final_plan)

            final_answer = session_state["final_answer"]
            tool_usage = session_state["tool_usage"]

            self.log_tool_stats(tool_usage)

            tools_executed = []
            for step in session_state["final_steps"]:
                if step.get("type") == "CODE":
                    code = step.get("code", {})
                    code_str = code.get("tool_arguments", {}).get("code", "")
                    tool_matches = re.findall(r'(\w+)\s*\(', code_str)
                    if tool_matches:
                        tool_name = tool_matches[0]
                        status = step.get("execution_result", {}).get("status", "unknown")
                        tools_executed.append(f"{tool_name}({status})")

            tools_executed_str = ", ".join(tools_executed) if tools_executed else "No tools executed"

            return {
                "query": query,
                "tools": tools,
                "complexity": complexity,
                "final_plan": final_plan,
                "output": final_answer,
                "tool_usage": tool_usage,
                "tools_executed": tools_executed_str
            }

        except Exception as e:
            logger.error(f"Error executing query '{query}': {str(e)}")
            return {
                "query": query,
                "tools": tools,
                "complexity": complexity,
                "final_plan": f"Error: {str(e)}",
                "output": "Failed to execute query",
                "tool_usage": [],
                "tools_executed": "Error Occurred"
            }

async def main():
    try:
        tester = await QueryTester.create()

        with open(OUTPUT_FILE, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['Query', 'Tools Needed', 'Complexity', 'Final Plan', 'Output', 'Tools Executed'])
            writer.writeheader()

        with open(INPUT_FILE, 'r') as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if i >= NUM_QUERIES:
                    break

                query = row['Query']
                tools = row['Tools Needed']
                complexity = row['Complexity']

                print(f"🔍 Processing query {i+1}/{NUM_QUERIES}: {query}\n")
                result = await tester.execute_query(query, tools, complexity)

                with open(OUTPUT_FILE, 'a', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=['Query', 'Tools Needed', 'Complexity', 'Final Plan', 'Output', 'Tools Executed'])
                    writer.writerow({
                        'Query': result['query'],
                        'Tools Needed': result['tools'],
                        'Complexity': result['complexity'],
                        'Final Plan': result['final_plan'],
                        'Output': result['output'],
                        'Tools Executed': result['tools_executed']
                    })

                if i < NUM_QUERIES - 1:
                    print(f"\n⌛ Sleeping for {SLEEP_TIME} seconds...\n")
                    await asyncio.sleep(SLEEP_TIME)

        tester.write_tool_performance_report()
        print(f"All queries processed. Results written to {OUTPUT_FILE}")

    except Exception as e:
        logger.error(f"Error in main: {str(e)}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
