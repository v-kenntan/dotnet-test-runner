import { useEffect, useRef, useMemo, useImperativeHandle, forwardRef } from 'react'
import AnsiToHtml from 'ansi-to-html'

interface Props {
  logs: string[];
}

export interface LogViewerHandle {
  scrollToTest: (testTitle: string) => void;
}

const ansiConverter = new AnsiToHtml({
  fg: '#eee',
  bg: 'transparent',
  newline: false,
  escapeXML: true,
  colors: {
    0: '#555',
    1: '#f44336',
    2: '#4caf50',
    3: '#ff9800',
    4: '#2196f3',
    5: '#e91e63',
    6: '#00bcd4',
    7: '#eee',
  }
});

const LogViewer = forwardRef<LogViewerHandle, Props>(({ logs }, ref) => {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [logs]);

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
    <div className="log-viewer" ref={containerRef}>
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
