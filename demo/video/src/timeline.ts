// Shape of demo/out/timeline.json, produced by demo/compose-timeline.cjs.
export type SceneKind = 'title' | 'plate' | 'sources' | 'clip' | 'photo' | 'end';

export type Scene = {
  id: string;
  kind: SceneKind;
  caption: string;
  samplePill: boolean;
  fadeIn: boolean;
  startFrame: number;
  contentFrames: number;
  narrationSeconds: number;
  audio: string | null;
  clip?: string;
  trimStart?: number;
  asset?: string;
  chips?: string[];
};

export type Tokens = Record<string, string>;

export type Timeline = {
  fps: number;
  width: number;
  height: number;
  voiced: boolean;
  tokens: Tokens;
  totalFrames: number;
  crossfadeFrames: number;
  scenes: Scene[];
};

// A minimal stub so Remotion Studio opens without a rendered-out directory.
export const stubTimeline: Timeline = {
  fps: 30,
  width: 1920,
  height: 1080,
  voiced: false,
  tokens: {
    paper: '#f6f1e7',
    'paper-raised': '#fcf9f2',
    'paper-sunken': '#ede6d8',
    ink: '#26201a',
    'ink-soft': '#5b5142',
    line: '#d9cfbc',
    'line-strong': '#b7aa92',
    accent: '#94472e',
    'accent-strong': '#7a3822',
    'accent-soft': '#f2e3d6',
    'accent-ink': '#fcf9f2',
    serif: '"Iowan Old Style", "Palatino Nova", Palatino, "Book Antiqua", Georgia, serif',
    radius: '10px',
    shadow: '0 1px 2px rgb(43 35 26 / 0.06), 0 6px 24px rgb(43 35 26 / 0.09)',
  },
  totalFrames: 300,
  crossfadeFrames: 12,
  scenes: [
    {
      id: 'title', kind: 'title', caption: '', samplePill: false, fadeIn: false,
      startFrame: 0, contentFrames: 150, narrationSeconds: 0, audio: null,
    },
    {
      id: 'sources', kind: 'sources', caption: 'Use one source, several, or none.',
      samplePill: false, fadeIn: true, startFrame: 138, contentFrames: 162,
      narrationSeconds: 0, audio: null,
      chips: ['iNaturalist', 'eBird', 'BirdWeather'],
    },
  ],
};
