import os
import json
import uuid
import datetime
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai.errors import ServerError

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

class Perception:
    def __init__(self, perception_prompt_path: str, api_key: str | None = None, model: str = "gemini-2.0-flash"):
        load_dotenv()
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY not found in environment or explicitly provided.")
        self.client = genai.Client(api_key=self.api_key)
        self.perception_prompt_path = perception_prompt_path

    def build_perception_input(self, raw_input: str, memory: list, current_plan = "", snapshot_type: str = "user_query") -> dict:
        if memory:
            memory_excerpt = {
                f"memory_{i+1}": {
                    "query": res["query"],
                    "result_requirement": res["result_requirement"],
                    "solution_summary": res["solution_summary"]
                }
                for i, res in enumerate(memory)}
        else:
            memory_excerpt = {}

        return {
            "run_id": str(uuid.uuid4()),
            "snapshot_type": snapshot_type,
            "raw_input": raw_input,
            "memory_excerpt": memory_excerpt,
            "prev_objective": "",
            "prev_confidence": None,
            "timestamp": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "schema_version": 1,
            "current_plan" : current_plan or "Initial Query Mode, plan not created"
        }
    
    def run(self, perception_input: dict) -> dict:
        """Run perception on given input using the specified prompt file."""
        prompt_template = Path(self.perception_prompt_path).read_text(encoding="utf-8")
        full_prompt = f"{prompt_template.strip()}\n\n```json\n{json.dumps(perception_input, indent=2)}\n```"

        try:
            response = self.client.models.generate_content(
                model="gemini-2.0-flash",
                contents=full_prompt
            )
        except ServerError as e:
            print(f"🚫 Perception LLM ServerError: {e}")
            return {
                "step_index": 0,
                "description": "Perception model unavailable: server overload.",
                "type": "NOP",
                "code": "",
                "conclusion": "",
                "plan_text": ["Step 0: Perception model returned a 503. Exiting to avoid loop."],
                "raw_text": str(e)
            }

        raw_text = response.text.strip()

        try:
            json_block = raw_text.split("```json")[1].split("```")[0].strip()

            # Minimal sanitization — no unicode decoding
            output = json.loads(json_block)

            # ✅ Patch missing fields for PerceptionSnapshot
            required_fields = {
                "entities": [],
                "result_requirement": "No requirement specified.",
                "original_goal_achieved": False,
                "reasoning": "No reasoning given.",
                "local_goal_achieved": False,
                "local_reasoning": "No local reasoning given.",
                "last_tooluse_summary": "None",
                "solution_summary": "No summary.",
                "confidence": "0.0"
            }

            for key, default in required_fields.items():
                output.setdefault(key, default)

            return output

        except Exception as e:
            # Optional: log to disk for inspection
            # import pdb; pdb.set_trace()

            print("❌ EXCEPTION IN PERCEPTION:", e)
            return {
                "entities": [],
                "result_requirement": "N/A",
                "original_goal_achieved": False,
                "reasoning": "Perception failed to parse model output as JSON.",
                "local_goal_achieved": False,
                "local_reasoning": "Could not extract structured information.",
                "solution_summary": "Not ready yet",
                "confidence": "0.0"
            }
            
    def get_human_input(self, prompt: str, current_plan: list = None) -> str:
        """Get input from human to resolve a clarification or tool failure."""
        print("\n🧑‍💼 Human-In-Loop Required")
        if current_plan:
            print("\nCurrent plan:")
            for step in current_plan:
                print(f"  {step}")
        
        print(f"\n{prompt}")
        try:
            human_response = input("\n🟢 Your response: ").strip()
            return human_response
        except Exception as e:
            print(f"\n⚠️ Error getting human input: {e}")
            return "Error occurred while getting input, please try again."
    
    def handle_tool_failure(self, error_details: str, step_description: str, current_plan: list) -> dict:
        """Handle tool failure by requesting human input."""
        try:
            prompt = f"⚠️ Tool execution failed during step: {step_description}\n\nError: {error_details}\n\nPlease provide guidance on how to proceed:"
            human_input = self.get_human_input(prompt, current_plan)
            
            return {
                "entities": [],
                "result_requirement": "Human-assisted recovery from tool failure",
                "original_goal_achieved": False,  # Let the agent decide based on the human input
                "reasoning": f"Tool failed but received human assistance: {human_input}",
                "local_goal_achieved": True,  # Allow agent to continue with next step
                "local_reasoning": f"Human provided input to address tool failure: {human_input}",
                "last_tooluse_summary": f"Tool failed, human intervention requested and received",
                "solution_summary": f"Human input: {human_input}",
                "confidence": "0.8",  # Higher confidence since human provided input
                "human_input": human_input  # Add human input for agent's context
            }
        except Exception as e:
            print(f"\n⚠️ Error in handle_tool_failure: {e}")
            # Provide fallback data
            return {
                "entities": [],
                "result_requirement": "Error recovery required",
                "original_goal_achieved": False,
                "reasoning": f"Error occurred while getting human assistance: {e}",
                "local_goal_achieved": False,  # Maybe don't continue automatically after an error
                "local_reasoning": "Error in human-in-loop functionality",
                "last_tooluse_summary": f"Tool failed, and human intervention also failed: {e}",
                "solution_summary": "System encountered an error while getting human input",
                "confidence": "0.3",
                "human_input": "Error occurred while getting human input"
            }
        
    def handle_nop_clarification(self, description: str, current_plan: list) -> dict:
        """Handle NOP step by requesting human clarification."""
        try:
            prompt = f"❓ Clarification needed: {description}\n\nPlease provide your input:"
            human_input = self.get_human_input(prompt, current_plan)
            
            # Ensure non-empty input
            if not human_input:
                human_input = "No specific input provided"
            
            return {
                "entities": [],
                "result_requirement": "Human clarification for proceeding with the task",
                "original_goal_achieved": False,
                "reasoning": f"Agent requested clarification, human provided: {human_input}",
                "local_goal_achieved": True,  # Let agent continue with the human input
                "local_reasoning": f"Human clarification received for: {description}",
                "last_tooluse_summary": "N/A - clarification step",
                "solution_summary": f"Human clarification: {human_input}",
                "confidence": "0.85",
                "human_input": human_input
            }
        except Exception as e:
            print(f"\n⚠️ Error in handle_nop_clarification: {e}")
            # Provide fallback data
            return {
                "entities": [],
                "result_requirement": "Error recovery required",
                "original_goal_achieved": False,
                "reasoning": f"Error occurred while getting human clarification: {e}",
                "local_goal_achieved": False,  # Maybe don't continue automatically after an error
                "local_reasoning": "Error in human-in-loop clarification",
                "last_tooluse_summary": "Error during clarification step",
                "solution_summary": "System encountered an error while getting clarification",
                "confidence": "0.3",
                "human_input": "Error occurred while getting human clarification"
            }

