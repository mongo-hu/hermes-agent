"""Persistent runtime orchestration for DFM analyzers.

Runtime submodules deliberately avoid eager re-exports so worker process
primitives can be imported without loading the analyzer registry.
"""
