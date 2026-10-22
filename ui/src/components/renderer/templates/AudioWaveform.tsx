import React, { useRef, useState, useEffect } from 'react';
import type { RendererProps } from '../types';

interface Segment { start: number; end: number; text: string; }
interface AudioWaveformData {
  src: string;
  transcript?: string;
  segments?: Segment[];
}

export default function AudioWaveform({ data, theme: t, onInteraction }: RendererProps<AudioWaveformData>) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const onTime = () => setCurrentTime(audio.currentTime);
    const onLoaded = () => setDuration(audio.duration);
    audio.addEventListener('timeupdate', onTime);
    audio.addEventListener('loadedmetadata', onLoaded);
    return () => { audio.removeEventListener('timeupdate', onTime); audio.removeEventListener('loadedmetadata', onLoaded); };
  }, []);

  const seekTo = (time: number) => {
    if (audioRef.current) { audioRef.current.currentTime = time; audioRef.current.play(); }
    onInteraction?.({ type: 'scrub', target: 'audio', data: { time }, timestamp: Date.now() });
  };

  const formatTime = (s: number) => `${Math.floor(s / 60)}:${Math.floor(s % 60).toString().padStart(2, '0')}`;
  const progress = duration > 0 ? (currentTime / duration) * 100 : 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <audio ref={audioRef} src={data.src} style={{ display: 'none' }} />
      <div style={{ background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, padding: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
          <button onClick={() => audioRef.current?.paused ? audioRef.current?.play() : audioRef.current?.pause()}
            style={{ width: 36, height: 36, borderRadius: '50%', border: 'none', background: t.colors.primary, color: 'white', cursor: 'pointer', fontSize: 14, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            {audioRef.current?.paused !== false ? '▶' : '⏸'}
          </button>
          <div style={{ flex: 1 }}>
            <div style={{ height: 4, background: t.colors.border, borderRadius: 2, cursor: 'pointer', position: 'relative' }}
              onClick={e => { const rect = e.currentTarget.getBoundingClientRect(); seekTo((e.clientX - rect.left) / rect.width * duration); }}>
              <div style={{ height: '100%', background: t.colors.primary, borderRadius: 2, width: `${progress}%`, transition: 'width 0.1s linear' }} />
            </div>
          </div>
          <span style={{ fontSize: t.typography.sizes.meta, color: t.colors.textSecondary, fontFamily: t.typography.fontMono }}>
            {formatTime(currentTime)} / {formatTime(duration)}
          </span>
        </div>
      </div>
      {data.segments && data.segments.length > 0 && (
        <div style={{ background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, overflow: 'hidden', maxHeight: 200, overflowY: 'auto' }}>
          {data.segments.map((seg, idx) => (
            <div key={idx} onClick={() => seekTo(seg.start)}
              style={{ padding: '8px 12px', cursor: 'pointer', borderBottom: idx < data.segments!.length - 1 ? `1px solid ${t.colors.border}` : undefined, background: currentTime >= seg.start && currentTime < seg.end ? t.colors.activeBg : 'transparent' }}>
              <span style={{ fontSize: t.typography.sizes.meta, color: t.colors.primary, fontFamily: t.typography.fontMono, marginRight: 8 }}>{formatTime(seg.start)}</span>
              <span style={{ fontSize: 12, color: t.colors.textPrimary }}>{seg.text}</span>
            </div>
          ))}
        </div>
      )}
      {data.transcript && !data.segments && (
        <div style={{ background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, padding: 12, fontSize: 12, color: t.colors.textPrimary, lineHeight: 1.6, maxHeight: 200, overflowY: 'auto' }}>
          {data.transcript}
        </div>
      )}
    </div>
  );
}
