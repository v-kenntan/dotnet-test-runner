const API_BASE = '/api';

export interface TestCase {
  id: string;
  category: string;
  title: string;
  description: string;
  steps: Step[];
  is_builtin: boolean;
  is_machine_mutating: boolean;
}

export interface Step {
  type: 'command' | 'write_file' | 'assert_output';
  command?: string;
  path?: string;
  content?: string;
  timeout?: number;
  expected_exit_code?: number | number[];
  assert_output_contains?: string[];
  continue_on_error?: boolean;
}

export interface TestRun {
  id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  environment_info: string;
  summary: string | null;
  sdk_version: string | null;
}

export interface StreamEvent {
  type: string;
  [key: string]: unknown;
}

export async function fetchTests(): Promise<TestCase[]> {
  const res = await fetch(`${API_BASE}/tests`);
  return res.json();
}

export async function createTest(test: Partial<TestCase>): Promise<{ id: string }> {
  const res = await fetch(`${API_BASE}/tests`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(test),
  });
  return res.json();
}

export async function updateTest(id: string, test: Partial<TestCase>): Promise<void> {
  await fetch(`${API_BASE}/tests/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(test),
  });
}

export async function deleteTest(id: string): Promise<void> {
  await fetch(`${API_BASE}/tests/${id}`, { method: 'DELETE' });
}

export async function startExecution(testIds: string[], sdkVersion?: string): Promise<{ run_id: string }> {
  const res = await fetch(`${API_BASE}/execute`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ test_ids: testIds, sdk_version: sdkVersion || null }),
  });
  return res.json();
}

export async function cancelExecution(runId: string): Promise<void> {
  await fetch(`${API_BASE}/execute/${runId}/cancel`, { method: 'POST' });
}

export function streamExecution(runId: string, onEvent: (event: StreamEvent) => void): () => void {
  const eventSource = new EventSource(`${API_BASE}/execute/${runId}/stream`);
  eventSource.onmessage = (e) => {
    const event = JSON.parse(e.data);
    onEvent(event);
    if (event.type === 'run_end') {
      eventSource.close();
    }
  };
  eventSource.onerror = () => {
    eventSource.close();
  };
  return () => eventSource.close();
}

export async function fetchRuns(): Promise<TestRun[]> {
  const res = await fetch(`${API_BASE}/runs`);
  return res.json();
}

export async function fetchRunDetails(runId: string): Promise<{ run: TestRun; results: unknown[] }> {
  const res = await fetch(`${API_BASE}/runs/${runId}`);
  return res.json();
}

export async function fetchStepResults(runId: string, resultId: string): Promise<unknown[]> {
  const res = await fetch(`${API_BASE}/runs/${runId}/results/${resultId}/steps`);
  return res.json();
}

export async function fetchEnvironment(): Promise<{ output: string; exit_code: number }> {
  const res = await fetch(`${API_BASE}/environment`);
  return res.json();
}

export interface SdkEntry {
  version: string;
  path: string;
}

export async function fetchSdks(): Promise<{ sdks: SdkEntry[]; error?: string }> {
  const res = await fetch(`${API_BASE}/sdks`);
  return res.json();
}
