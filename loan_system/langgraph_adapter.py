"""OPTIONAL: expose the same workflow as a LangGraph StateGraph.

The project runs on its own small graph runner so it works with zero extra
installs. If you `pip install langgraph`, build_langgraph() re-creates the exact
same nodes/edges as a LangGraph graph, so the design maps 1:1 onto it.

NOTE: written against LangGraph's public StateGraph API but not exercised in the
offline environment this project was built in. Treat it as a starting point.
"""
from __future__ import annotations

from typing import TypedDict

from .orchestrator import END, PAUSE, LoanWorkflow
from .state import WorkflowContext


class GraphState(TypedDict):
    ctx: WorkflowContext


def build_langgraph(workflow: LoanWorkflow):
    try:
        from langgraph.graph import END as LG_END, StateGraph
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pip install langgraph to use this adapter") from exc

    g = workflow.graph
    sg = StateGraph(GraphState)
    for name in g.handlers:
        def node(state: GraphState, _name=name) -> GraphState:
            g.run_node(_name, state["ctx"])
            return state
        sg.add_node(name, node)

    sg.set_entry_point(g.entry)
    for src in g.handlers:
        targets = {name: name for name in g.handlers}
        targets.update({END: LG_END, PAUSE: LG_END})     # a pause simply ends this run

        def router(state: GraphState, _src=src) -> str:
            return g.next_node(_src, state["ctx"])
        sg.add_conditional_edges(src, router, targets)
    return sg.compile()
