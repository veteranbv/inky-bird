import React from 'react';
import {
  AbsoluteFill,
  Audio,
  Easing,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
} from 'remotion';
import type { Scene, Timeline, Tokens } from './timeline';

// One easing curve for every animation in the film.
const EASE = Easing.bezier(0.3, 0.1, 0.25, 1);

const ease = (frame: number, range: [number, number], to: [number, number]) =>
  interpolate(frame, range, to, {
    easing: EASE,
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

const Brand: React.FC<{ tokens: Tokens; size: number }> = ({ tokens, size }) => (
  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: size * 0.17 }}>
    <h1
      style={{
        margin: 0,
        font: `700 ${size}px/1.05 ${tokens.serif}`,
        letterSpacing: '-0.025em',
        color: tokens.ink,
      }}
    >
      Inky Bird
    </h1>
    <Img src={staticFile('demo/inky-bird-mark.png')} style={{ height: size * 0.72, width: 'auto' }} />
  </div>
);

const TitleCard: React.FC<{ tokens: Tokens }> = ({ tokens }) => {
  const frame = useCurrentFrame();
  const scale = ease(frame, [0, 150], [1, 1.03]);
  return (
    <AbsoluteFill style={{ background: tokens.paper, alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ transform: `scale(${scale})`, textAlign: 'center' }}>
        <Brand tokens={tokens} size={168} />
        <p style={{ margin: '42px 0 0', font: `400 44px/1.4 ${tokens.serif}`, color: tokens['ink-soft'] }}>
          A field journal for the birds in your life.
        </p>
      </div>
    </AbsoluteFill>
  );
};

const PlateScene: React.FC<{ tokens: Tokens; scene: Scene }> = ({ tokens, scene }) => {
  const frame = useCurrentFrame();
  const scale = ease(frame, [0, scene.contentFrames], [1.06, 1.18]);
  return (
    <AbsoluteFill style={{ background: tokens['paper-sunken'] }}>
      <Img
        src={staticFile(scene.asset as string)}
        style={{
          width: '100%',
          height: '100%',
          objectFit: 'cover',
          objectPosition: '62% 16%',
          transform: `scale(${scale})`,
          transformOrigin: '60% 28%',
        }}
      />
    </AbsoluteFill>
  );
};

const SourcesScene: React.FC<{ tokens: Tokens; scene: Scene }> = ({ tokens, scene }) => {
  const frame = useCurrentFrame();
  const chips = scene.chips ?? [];
  return (
    <AbsoluteFill style={{ background: tokens.paper, alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ width: 1280, textAlign: 'center' }}>
        <p
          style={{
            margin: 0,
            font: '600 26px/1 system-ui, sans-serif',
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
            color: tokens.accent,
            opacity: ease(frame, [0, 12], [0, 1]),
          }}
        >
          Sources
        </p>
        <h2
          style={{
            margin: '26px 0 54px',
            font: `700 72px/1.15 ${tokens.serif}`,
            color: tokens.ink,
            opacity: ease(frame, [4, 18], [0, 1]),
            transform: `translateY(${ease(frame, [4, 18], [14, 0])}px)`,
          }}
        >
          Where your birds come from
        </h2>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 26, justifyContent: 'center' }}>
          {chips.map((chip, index) => {
            const start = 14 + index * 7;
            return (
              <div
                key={chip}
                style={{
                  font: '600 34px/1 system-ui, sans-serif',
                  color: tokens.ink,
                  background: tokens['paper-raised'],
                  border: `1.5px solid ${tokens.line}`,
                  borderRadius: 999,
                  padding: '22px 40px',
                  boxShadow: tokens.shadow,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 18,
                  opacity: ease(frame, [start, start + 12], [0, 1]),
                  transform: `translateY(${ease(frame, [start, start + 12], [22, 0])}px)`,
                }}
              >
                <span
                  style={{
                    width: 14,
                    height: 14,
                    borderRadius: 99,
                    background: tokens.accent,
                    display: 'inline-block',
                  }}
                />
                {chip}
              </div>
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
};

// The app clip sits in a raised card with a caption band underneath, so a
// caption never covers the interface it is talking about.
const CLIP_CARD = { width: 1680, height: 945, top: 52 };

const ClipScene: React.FC<{ tokens: Tokens; scene: Scene; fps: number }> = ({ tokens, scene, fps }) => (
  <AbsoluteFill style={{ background: tokens.paper }}>
    <div
      style={{
        position: 'absolute',
        left: (1920 - CLIP_CARD.width) / 2,
        top: CLIP_CARD.top,
        width: CLIP_CARD.width,
        height: CLIP_CARD.height,
        borderRadius: 20,
        overflow: 'hidden',
        border: `1px solid ${tokens.line}`,
        boxShadow: tokens.shadow,
        background: tokens['paper-raised'],
      }}
    >
      <OffthreadVideo
        muted
        src={staticFile(scene.clip as string)}
        startFrom={Math.round((scene.trimStart ?? 0) * fps)}
        style={{ width: '100%', height: '100%' }}
      />
    </div>
  </AbsoluteFill>
);

const PhotoScene: React.FC<{ scene: Scene }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const scale = ease(frame, [0, scene.contentFrames], [1, 1.05]);
  return (
    <AbsoluteFill style={{ background: '#15120d', alignItems: 'center', justifyContent: 'center' }}>
      <Img
        src={staticFile(scene.asset as string)}
        style={{
          height: '94%',
          width: 'auto',
          objectFit: 'contain',
          transform: `scale(${scale})`,
          transformOrigin: '50% 42%',
          boxShadow: '0 18px 70px rgba(0, 0, 0, 0.55)',
        }}
      />
    </AbsoluteFill>
  );
};

const EndCard: React.FC<{ tokens: Tokens }> = ({ tokens }) => {
  const frame = useCurrentFrame();
  const line = (index: number) => ({
    opacity: ease(frame, [8 + index * 7, 22 + index * 7], [0, 1]),
    transform: `translateY(${ease(frame, [8 + index * 7, 22 + index * 7], [12, 0])}px)`,
  });
  return (
    <AbsoluteFill style={{ background: tokens.paper, alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ textAlign: 'center' }}>
        <Brand tokens={tokens} size={108} />
        <p style={{ margin: '48px 0 0', font: `400 42px/1.5 ${tokens.serif}`, color: tokens.ink, ...line(0) }}>
          Browse the gallery · henrysowell.com/birds
        </p>
        <p style={{ margin: '16px 0 0', font: '400 32px/1.5 system-ui, sans-serif', color: tokens['ink-soft'], ...line(1) }}>
          github.com/veteranbv/inky-bird
        </p>
        <p style={{ margin: '28px 0 0', font: '600 28px/1.5 system-ui, sans-serif', color: tokens.accent, ...line(2) }}>
          MIT licensed · self-hosted
        </p>
      </div>
    </AbsoluteFill>
  );
};

const Caption: React.FC<{ tokens: Tokens; text: string; band: boolean }> = ({ tokens, text, band }) => {
  const frame = useCurrentFrame();
  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        right: 0,
        bottom: band ? 10 : 44,
        display: 'flex',
        justifyContent: 'center',
        opacity: ease(frame, [6, 18], [0, 1]),
        transform: `translateY(${ease(frame, [6, 18], [16, 0])}px)`,
      }}
    >
      <div
        style={{
          maxWidth: 1600,
          font: `400 ${band ? 32 : 38}px/1.3 ${tokens.serif}`,
          color: tokens.ink,
          background: band ? 'transparent' : 'rgba(252, 249, 242, 0.94)',
          border: band ? 'none' : `1px solid ${tokens.line}`,
          borderRadius: tokens.radius,
          boxShadow: band ? 'none' : tokens.shadow,
          padding: band ? '14px 36px' : '18px 40px',
          textAlign: 'center',
        }}
      >
        {text}
      </div>
    </div>
  );
};

const SamplePill: React.FC<{ tokens: Tokens }> = ({ tokens }) => (
  <div
    style={{
      position: 'absolute',
      top: 30,
      right: 104,
      font: '600 24px/1 system-ui, sans-serif',
      letterSpacing: '0.025em',
      color: tokens.accent,
      background: tokens['accent-soft'],
      border: `1px solid ${tokens.line}`,
      borderRadius: 999,
      padding: '10px 22px',
    }}
  >
    Sample data
  </div>
);

const SceneBody: React.FC<{ scene: Scene; tokens: Tokens; fps: number }> = ({ scene, tokens, fps }) => {
  switch (scene.kind) {
    case 'title':
      return <TitleCard tokens={tokens} />;
    case 'plate':
      return <PlateScene tokens={tokens} scene={scene} />;
    case 'sources':
      return <SourcesScene tokens={tokens} scene={scene} />;
    case 'clip':
      return <ClipScene tokens={tokens} scene={scene} fps={fps} />;
    case 'photo':
      return <PhotoScene scene={scene} />;
    case 'end':
      return <EndCard tokens={tokens} />;
    default:
      return null;
  }
};

const SceneLayer: React.FC<{ scene: Scene; timeline: Timeline }> = ({ scene, timeline }) => {
  const frame = useCurrentFrame();
  const opacity = scene.fadeIn
    ? ease(frame, [0, timeline.crossfadeFrames], [0, 1])
    : 1;
  return (
    <AbsoluteFill style={{ opacity }}>
      <SceneBody scene={scene} tokens={timeline.tokens} fps={timeline.fps} />
      {scene.samplePill ? <SamplePill tokens={timeline.tokens} /> : null}
      {scene.caption ? (
        <Caption tokens={timeline.tokens} text={scene.caption} band={scene.kind === 'clip'} />
      ) : null}
      {scene.audio ? (
        <Sequence from={6}>
          <Audio src={staticFile(scene.audio)} />
        </Sequence>
      ) : null}
    </AbsoluteFill>
  );
};

export const Film: React.FC<{ timeline: Timeline }> = ({ timeline }) => (
  <AbsoluteFill style={{ background: timeline.tokens.paper }}>
    {timeline.scenes.map((scene) => (
      <Sequence key={scene.id} from={scene.startFrame} durationInFrames={scene.contentFrames}>
        <SceneLayer scene={scene} timeline={timeline} />
      </Sequence>
    ))}
  </AbsoluteFill>
);
