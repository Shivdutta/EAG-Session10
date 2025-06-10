import uuid
import json
import datetime
from perception.perception import Perception
from decision.decision import Decision
from action.executor import run_user_code
from agent.agentSession import AgentSession, PerceptionSnapshot, Step, ToolCode
from memory.session_log import live_update_session
from memory.memory_search import MemorySearch
from mcp_servers.multiMCP import MultiMCP


GLOBAL_PREVIOUS_FAILURE_STEPS = 3

class AgentLoop:
    def __init__(self, perception_prompt_path: str, decision_prompt_path: str, multi_mcp: MultiMCP, strategy: str = "exploratory"):
        self.perception = Perception(perception_prompt_path)
        self.decision = Decision(decision_prompt_path, multi_mcp)
        self.multi_mcp = multi_mcp
        self.strategy = strategy

    async def run(self, query: str):
        session = AgentSession(session_id=str(uuid.uuid4()), original_query=query)
        session_memory= []
        self.log_session_start(session, query)        
        memory_results = self.search_memory(query)
        perception_result = self.run_perception(query, memory_results, memory_results)
        session.add_perception(PerceptionSnapshot(**perception_result))
        
        if perception_result.get("original_goal_achieved"):
            self.handle_perception_completion(session, perception_result)
            return session
            
        decision_output = self.make_initial_decision(query, perception_result)
        step = session.add_plan_version(decision_output["plan_text"], [self.create_step(decision_output)])
        live_update_session(session)
        print(f"\n[Decision Plan Text: V{len(session.plan_versions)}]:")
        for line in session.plan_versions[-1]["plan_text"]:
            print(f"  {line}")
            
        while step:
            # Execute the current step
            step_result = await self.execute_step(step, session, session_memory)
            
            # Safety check: if step_result is None, we're done
            if step_result is None:
                break  # 🔐 protect against CONCLUDE/NOP cases
                
            # Safety check: ensure the step has perception data
            if step_result.perception is None:
                print("\n⚠️ Step result missing perception data, generating default perception...")
                
                # Generate default perception based on step type and result
                if step_result.type == "CODE" and hasattr(step_result.execution_result, 'get'):
                    perception_input = step_result.execution_result.get('result', 'No result')
                elif step_result.type == "CONCLUDE":
                    perception_input = step_result.conclusion or "No conclusion provided"
                else:
                    perception_input = step_result.description
                
                perception_result = self.run_perception(
                    query=perception_input,
                    memory_results=session_memory,
                    current_plan=session.plan_versions[-1]["plan_text"],
                    snapshot_type="step_result"
                )
                step_result.perception = PerceptionSnapshot(**perception_result)
                
            # Now evaluate the step with valid perception data
            step = self.evaluate_step(step_result, session, query)

        return session

    def log_session_start(self, session, query):
        print("\n=== LIVE AGENT SESSION TRACE ===")
        print(f"Session ID: {session.session_id}")
        print(f"Query: {query}")

    def search_memory(self, query):
        print("Searching Recent Conversation History")
        searcher = MemorySearch()
        results = searcher.search_memory(query)
        if not results:
            print("❌ No matching memory entries found.\n")
        else:
            print("\n🎯 Top Matches:\n")
            for i, res in enumerate(results, 1):
                print(f"[{i}] File: {res['file']}\nQuery: {res['query']}\nResult Requirement: {res['result_requirement']}\nSummary: {res['solution_summary']}\n")
        return results

    def run_perception(self, query, memory_results, session_memory=None, snapshot_type="user_query", current_plan=None):
        combined_memory = (memory_results or []) + (session_memory or [])
        perception_input = self.perception.build_perception_input(
            raw_input=query, 
            memory=combined_memory, 
            current_plan=current_plan, 
            snapshot_type=snapshot_type
        )
        perception_result = self.perception.run(perception_input)
        print("\n[Perception Result]:")
        print(json.dumps(perception_result, indent=2, ensure_ascii=False))
        return perception_result

    def handle_perception_completion(self, session, perception_result):
        print("\n✅ Perception fully answered the query.")
        session.state.update({
            "original_goal_achieved": True,
            "final_answer": perception_result.get("solution_summary", "Answer ready."),
            "confidence": perception_result.get("confidence", 0.95),
            "reasoning_note": perception_result.get("reasoning", "Handled by perception."),
            "solution_summary": perception_result.get("solution_summary", "Answer ready.")
        })
        live_update_session(session)

    def make_initial_decision(self, query, perception_result):
        decision_input = {
            "plan_mode": "initial",
            "planning_strategy": self.strategy,
            "original_query": query,
            "perception": perception_result
        }
        decision_output = self.decision.run(decision_input)
        return decision_output

    def create_step(self, decision_output):

        return Step(
            index=decision_output["step_index"],
            description=decision_output["description"],
            type=decision_output["type"],
            code=ToolCode(tool_name="raw_code_block", tool_arguments={"code": decision_output["code"]}) if decision_output["type"] == "CODE" else None,
            conclusion=decision_output.get("conclusion"),
        )

    async def execute_step(self, step, session, session_memory):
        print(f"\n[Step {step.index}] {step.description}")

        if step.type == "CODE":
            print("-" * 50, "\n[EXECUTING CODE]\n", step.code.tool_arguments["code"])
            executor_response = await run_user_code(step.code.tool_arguments["code"], self.multi_mcp)
            step.execution_result = executor_response
            # import pdb; pdb.set_trace()
            step.status = "completed"

            # Check if the tool execution failed
            if executor_response.get('status') == 'error':
                print(f"\n⚠️ Tool execution failed: {executor_response.get('error', 'Unknown error')}")
                
                #Use for performance testing
                # Skip human-in-loop and insert automated failure response
                default_error = executor_response.get('error', 'Unknown error')
                step.execution_result = {
                    **executor_response,
                    'human_input': '',
                    'result': f"AUTOMATED FAILURE HANDLING: {default_error}"
                }
                #end

                #by pass for performance testing
                # # Use human-in-loop to get help with the tool failure
                # perception_result = self.perception.handle_tool_failure(
                #     error_details=executor_response.get('error', 'Unknown error'),
                #     step_description=step.description,
                #     current_plan=session.plan_versions[-1]["plan_text"]
                # )
                
                # # Add human input to execution result for context
                # human_input = perception_result.get('human_input', '')
                # step.execution_result = {
                #     **executor_response,
                #     'human_input': human_input,
                #     'result': f"HUMAN ASSISTANCE: {human_input}"
                # }
                #end
            else:
                # Normal case - tool executed successfully
                perception_result = self.run_perception(
                    query=executor_response.get('result', 'Tool Completed'),
                    memory_results=session_memory,
                    current_plan=session.plan_versions[-1]["plan_text"],
                    snapshot_type="step_result"
                )
            
            step.perception = PerceptionSnapshot(**perception_result)

            if not step.perception or not step.perception.local_goal_achieved:
                failure_memory = {
                    "query": step.description,
                    "result_requirement": "Tool failed",
                    "solution_summary": str(step.execution_result)[:300]
                }
                session_memory.append(failure_memory)

                if len(session_memory) > GLOBAL_PREVIOUS_FAILURE_STEPS:
                    session_memory.pop(0)

            live_update_session(session)
            return step

        elif step.type == "CONCLUDE":
            print(f"\n💡 Conclusion: {step.conclusion}")
            step.execution_result = step.conclusion
            step.status = "completed"

            perception_result = self.run_perception(
                query=step.conclusion,
                memory_results=session_memory,
                current_plan=session.plan_versions[-1]["plan_text"],
                snapshot_type="step_result"
            )
            step.perception = PerceptionSnapshot(**perception_result)
            session.mark_complete(step.perception, final_answer=step.conclusion)
            live_update_session(session)
            return None        
        
        elif step.type == "NOP":
            print(f"\n❓ Clarification needed: {step.description}")
            
            try:
                # Get human clarification for NOP step
                perception_result = self.perception.handle_nop_clarification(
                    description=step.description,
                    current_plan=session.plan_versions[-1]["plan_text"]
                )
                
                # Process the human input
                human_input = perception_result.get('human_input', '')
                step.execution_result = {
                    'status': 'success',
                    'result': f"HUMAN CLARIFICATION: {human_input}",
                    'human_input': human_input
                }
                step.status = "completed"
                
                # Ensure we have all required fields for perception
                required_fields = {
                    "entities": [],
                    "result_requirement": "Human clarification was requested and received",
                    "original_goal_achieved": False,
                    "reasoning": f"Human provided clarification: {human_input}",
                    "local_goal_achieved": True,  # Allow execution to continue
                    "local_reasoning": "Human input received for NOP step",
                    "last_tooluse_summary": "Human clarification step",
                    "solution_summary": f"Human input: {human_input}",
                    "confidence": "0.8"  # Higher confidence due to human input
                }
                
                # Add missing fields to perception result
                for key, default in required_fields.items():
                    perception_result.setdefault(key, default)
                    
                step.perception = PerceptionSnapshot(**perception_result)
            except Exception as e:
                print(f"⚠️ Error processing human input: {e}")
                # Provide fallback perception data in case of errors
                step.perception = PerceptionSnapshot(
                    entities=[],
                    result_requirement="Handle error in human input processing",
                    original_goal_achieved=False,
                    reasoning=f"Error occurred while processing human input: {e}",
                    local_goal_achieved=True,  # Allow execution to continue despite error
                    local_reasoning="Fallback after error in human input handling",
                    last_tooluse_summary="Error in human input processing",
                    solution_summary=f"Human input was provided but had processing error: {e}",
                    confidence="0.5"  # Lower confidence due to error
                )
            
            # Create a new decision based on the human input
            decision_output = self.decision.run({
                "plan_mode": "mid_session", 
                "planning_strategy": self.strategy,
                "original_query": human_input,  # Use human input as the new query
                "current_plan_version": len(session.plan_versions),
                "current_plan": session.plan_versions[-1]["plan_text"],
                "completed_steps": [s.to_dict() for s in session.plan_versions[-1]["steps"] if s.status == "completed"],
                "current_step": step.to_dict()
            })
              # Create a new plan with the first step from the decision
            new_step = self.create_step(decision_output)
            session.add_plan_version(decision_output["plan_text"], [new_step])
            
            print(f"\n[Decision Plan Text Based on Human Input: V{len(session.plan_versions)}]:")
            for line in session.plan_versions[-1]["plan_text"]:
                print(f"  {line}")
                
            # Create a basic perception for the new step to prevent NoneType errors
            # We'll use the perception results from the human clarification
            if not hasattr(new_step, 'perception') or new_step.perception is None:
                # Create a baseline perception with conservative values
                baseline_perception = {
                    "entities": [],
                    "result_requirement": f"Execute step from human input: {human_input}",
                    "original_goal_achieved": False,  # Conservative
                    "reasoning": f"Planning based on human clarification: {human_input}",
                    "local_goal_achieved": True,  # Allow execution
                    "local_reasoning": "Step created from human clarification",
                    "last_tooluse_summary": "Human-guided step",
                    "solution_summary": f"Planning based on human input: {human_input}",
                    "confidence": "0.8"  # Higher confidence due to human input
                }
                new_step.perception = PerceptionSnapshot(**baseline_perception)
                
            live_update_session(session)
            return new_step  # Return the new step for execution
    
    def evaluate_step(self, step, session, query):
        # import pdb; pdb.set_trace()
        
        # If perception is None, we need to run perception on this step first
        if step.perception is None:
            print("\n⚠️ Step has no perception data, running perception...")
            
            # Get input data for perception based on step type
            if step.type == "CODE" and hasattr(step.execution_result, 'get'):
                perception_input = step.execution_result.get('result', 'No result')
            elif step.type == "CONCLUDE":
                perception_input = step.conclusion or "No conclusion provided"
            else:
                perception_input = step.description
            
            # Run perception on the step
            perception_result = self.run_perception(
                query=perception_input,
                memory_results=[],
                current_plan=session.plan_versions[-1]["plan_text"],
                snapshot_type="step_result"
            )
            step.perception = PerceptionSnapshot(**perception_result)
        
        # Now we can safely check perception data
        if step.perception.original_goal_achieved:
            print("\n✅ Goal achieved.")
            session.mark_complete(step.perception)
            live_update_session(session)
            return None
        elif step.perception.local_goal_achieved:
            return self.get_next_step(session, query, step)
        else:
            print("\n🔁 Step unhelpful. Replanning.")
            
            # If there's human input from a previous step, incorporate it into the decision
            human_input = ""
            if hasattr(step.execution_result, 'get') and step.execution_result.get('human_input'):
                human_input = step.execution_result.get('human_input')
                query = f"{query}\n\nHuman clarification: {human_input}"
            
            decision_output = self.decision.run({
                "plan_mode": "mid_session",
                "planning_strategy": self.strategy,
                "original_query": query,
                "current_plan_version": len(session.plan_versions),
                "current_plan": session.plan_versions[-1]["plan_text"],
                "completed_steps": [s.to_dict() for s in session.plan_versions[-1]["steps"] if s.status == "completed"],
                "current_step": step.to_dict()
            })
            step = session.add_plan_version(decision_output["plan_text"], [self.create_step(decision_output)])

            print(f"\n[Decision Plan Text: V{len(session.plan_versions)}]:")
            for line in session.plan_versions[-1]["plan_text"]:
                print(f"  {line}")

            return step

    def get_next_step(self, session, query, step):
        next_index = step.index + 1
        total_steps = len(session.plan_versions[-1]["plan_text"])
        if next_index < total_steps:
            # If there's human input from a previous step, incorporate it
            human_input = ""
            if hasattr(step.execution_result, 'get') and step.execution_result.get('human_input'):
                human_input = step.execution_result.get('human_input')
                query = f"{query}\n\nWith additional context: {human_input}"
            
            decision_output = self.decision.run({
                "plan_mode": "mid_session",
                "planning_strategy": self.strategy,
                "original_query": query,
                "current_plan_version": len(session.plan_versions),
                "current_plan": session.plan_versions[-1]["plan_text"],
                "completed_steps": [s.to_dict() for s in session.plan_versions[-1]["steps"] if s.status == "completed"],
                "current_step": step.to_dict()
            })
            step = session.add_plan_version(decision_output["plan_text"], [self.create_step(decision_output)])

            print(f"\n[Decision Plan Text: V{len(session.plan_versions)}]:")
            for line in session.plan_versions[-1]["plan_text"]:
                print(f"  {line}")

            return step

        else:
            print("\n✅ No more steps.")
            return None