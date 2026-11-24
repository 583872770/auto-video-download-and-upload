import gc
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import patch

from webapp.worker.memory import WorkerMemory, MIB


class WorkerMemoryTests(unittest.TestCase):
    def memory(self, values):
        process = SimpleNamespace(memory_info=lambda:SimpleNamespace(rss=next(values)))
        with patch('webapp.worker.memory.psutil.Process', return_value=process):
            return WorkerMemory()

    def test_small_tasks_reuse_process_without_forcing_gc_or_trim(self):
        memory = self.memory(iter([80*MIB, 85*MIB]))
        with patch('webapp.worker.memory.gc.collect') as collect, patch('webapp.worker.memory.trim_allocator') as trim:
            with memory.task():pass
        collect.assert_not_called();trim.assert_not_called()
        self.assertFalse(memory.retire)

    def test_abandoned_task_graph_is_reclaimed_even_on_failure(self):
        class Graph:pass
        memory = self.memory(iter([80*MIB, 180*MIB, 90*MIB]))
        with patch('webapp.worker.memory.trim_allocator') as trim:
            with self.assertRaisesRegex(RuntimeError, 'task failed'):
                with memory.task():
                    graph = Graph();graph.loop = graph;ref = weakref.ref(graph)
                    del graph
                    self.assertIsNotNone(ref())
                    raise RuntimeError('task failed')
        self.assertIsNone(ref());trim.assert_called_once()
        self.assertFalse(memory.retire)

    def test_retained_large_worker_retires_only_after_task_settles(self):
        memory = self.memory(iter([80*MIB, 350*MIB, 300*MIB]))
        with patch('webapp.worker.memory.trim_allocator'),patch('webapp.diagnostics.event') as event:
            with memory.task():
                self.assertFalse(memory.retire)
            self.assertTrue(memory.retire)
        self.assertEqual(event.call_args.kwargs['reason'], 'retained_memory')
