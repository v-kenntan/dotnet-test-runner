import { useEffect, useRef, useMemo, useImperativeHandle, forwardRef } from 'react'
import { ansiConverter } from '../ansi'

interface Props {
  logs: string[];
  /** Follow new output. Off for finished runs, where the top is the useful end. */
  autoScroll?: boolean;
  /** Styling hook — the history view has its own log-pane layout. */
  className?: string;
}

export interface LogViewerHandle {
  scrollToTest: (testTitle: string) => void;
}

const LogViewer = forwardRef<LogViewerHandle, Props>(({ logs, autoScroll = true, className = 'log-viewer' }, ref) => {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [logs, autoScroll]);

  useImperativeHandle(ref, () => ({
    scrollToTest(testTitle: string) {
      if (!containerRef.current) return;
      const lines = containerRef.current.querySelectorAll('.log-line');
      for (let i = 0; i < lines.length; i++) {
        if (logs[i]?.includes(`━━━ ${testTitle} ━━━`)) {
          const line = lines[i] as HTMLElement;
          containerRef.current.scrollTop = line.offsetTop - containerRef.current.offsetTop;
          break;
        }
      }
    }
  }));

  const renderedLines = useMemo(() => {
    return logs.map((line) => ansiConverter.toHtml(line));
  }, [logs]);

  return (
    <div className={className} ref={containerRef}>
      <pre>
        {renderedLines.map((html, i) => (
          <div
            key={i}
            className={`log-line ${logs[i]?.includes('[STDERR]') ? 'stderr' : ''} ${logs[i]?.includes('→ failed') ? 'failed' : ''} ${logs[i]?.includes('→ passed') ? 'passed' : ''} ${logs[i]?.startsWith('$ ') ? 'command' : ''}`}
            dangerouslySetInnerHTML={{ __html: html }}
          />
        ))}
      </pre>
    </div>
  );
});

export default LogViewer;
