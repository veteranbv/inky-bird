import React from 'react';
import { Composition } from 'remotion';
import { Film } from './Film';
import { stubTimeline, type Timeline } from './timeline';

export const RemotionRoot: React.FC = () => (
  <Composition
    id="InkyDemo"
    component={Film}
    defaultProps={{ timeline: stubTimeline }}
    calculateMetadata={({ props }) => {
      const timeline = props.timeline as Timeline;
      return {
        durationInFrames: timeline.totalFrames,
        fps: timeline.fps,
        width: timeline.width,
        height: timeline.height,
      };
    }}
    durationInFrames={stubTimeline.totalFrames}
    fps={stubTimeline.fps}
    width={stubTimeline.width}
    height={stubTimeline.height}
  />
);
