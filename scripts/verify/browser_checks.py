"""Readiness checks shared by browser fixtures that deliberately delay requests."""


def wait_for_workbench_ready(page):
    # The job list loads after the initial selection is restored. Waiting for
    # visible controls alone can return during the earlier instance-config load.
    page.wait_for_function('''() => jobsSearchEpoch > 0 && !state.instanceLoading &&
        !state.polling && !startupPolling && !executionPolling &&
        !document.querySelector('#download-jobs-more').disabled''')
