import AnsiToHtml from 'ansi-to-html';

export const ansiConverter = new AnsiToHtml({
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
