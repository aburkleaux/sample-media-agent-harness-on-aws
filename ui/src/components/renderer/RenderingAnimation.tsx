import React, { useEffect, useState } from 'react';

/**
 * RenderingAnimation — shown in the Session Renderer panel while the Render
 * Agent is composing a view (between dispatch and the manifest arriving).
 *
 * Thematic to the media pipeline: an equalizer that "analyzes", a scan line
 * sweeping a faux timeline, and rotating status captions. Self-contained
 * (injects its own @keyframes) so it drops in anywhere with no global CSS.
 */

const MESSAGES = [
  'Analyzing artifacts…',
  'Selecting render skill…',
  'Extracting waveforms…',
  'Mapping detections…',
  'Composing timeline…',
  'Validating manifest…',
];

const KEYFRAMES = `
@keyframes ra-eq { 0%,100% { transform: scaleY(0.18); } 50% { transform: scaleY(1); } }
@keyframes ra-scan { 0% { left: -30%; } 100% { left: 130%; } }
@keyframes ra-dot { 0%,100% { opacity: 0.25; transform: scale(0.8); } 50% { opacity: 1; transform: scale(1); } }
@keyframes ra-fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
@keyframes ra-ring { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
`;

export default function RenderingAnimation({ label }: { label?: string }) {
  const [idx, setIdx] = useState(0);
  useEffect(() => {
    if (label) return; // fixed label overrides rotation
    const t = setInterval(() => setIdx((v) => (v + 1) % MESSAGES.length), 1400);
    return () => clearInterval(t);
  }, [label]);

  const bars = Array.from({ length: 11 });

  return (
    <div style={wrap}>
      <style>{KEYFRAMES}</style>

      {/* Stage: sweeping scan line behind an animated equalizer */}
      <div style={stage}>
        <div style={scan} />
        <div style={eqRow}>
          {bars.map((_, i) => (
            <span
              key={i}
              style={{
                ...bar,
                animationDelay: `${(i * 0.08).toFixed(2)}s`,
                background: `linear-gradient(180deg, #6ee7ff 0%, #7c5cff 60%, #e0518f 100%)`,
              }}
            />
          ))}
        </div>
        {/* orbiting ring accent */}
        <div style={ring} />
      </div>

      <div style={dotsRow}>
        {[0, 1, 2].map((i) => (
          <span key={i} style={{ ...dot, animationDelay: `${i * 0.2}s` }} />
        ))}
      </div>

      <div key={label || idx} style={msg}>{label || MESSAGES[idx]}</div>
      <div style={sub}>Render agent is composing your view</div>
    </div>
  );
}

const wrap: React.CSSProperties = {
  display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
  // flex:1 (not height:100%) so the card fills the panel: its container chain is sized by a
  // min-height:100% wrapper, which counts as an INDEFINITE height for percentage resolution, so
  // height:100% collapsed to minHeight and left dead space below. flex-grow distributes the
  // parent's USED height and is immune to that quirk; minHeight:240 floors it, and if ever
  // rendered outside a flex column it harmlessly falls back to that floor.
  gap: 14, flex: 1, minHeight: 240, padding: 24,
  background: 'radial-gradient(120% 120% at 50% 0%, #1c1c3a 0%, #12122a 70%)',
  borderRadius: 10,
};

const stage: React.CSSProperties = {
  position: 'relative', width: '80%', maxWidth: 320, height: 96,
  display: 'flex', alignItems: 'flex-end', justifyContent: 'center',
  overflow: 'hidden', borderRadius: 8,
};

const scan: React.CSSProperties = {
  position: 'absolute', top: 0, bottom: 0, width: '18%',
  background: 'linear-gradient(90deg, transparent, rgba(110,231,255,0.35), transparent)',
  animation: 'ra-scan 1.8s ease-in-out infinite', pointerEvents: 'none',
};

const eqRow: React.CSSProperties = {
  display: 'flex', alignItems: 'flex-end', gap: 5, height: '100%', zIndex: 1,
};

const bar: React.CSSProperties = {
  width: 8, height: '100%', borderRadius: 4,
  transformOrigin: 'bottom', animation: 'ra-eq 1s ease-in-out infinite',
};

const ring: React.CSSProperties = {
  position: 'absolute', top: 8, right: 8, width: 18, height: 18, borderRadius: '50%',
  border: '2px solid rgba(124,92,255,0.35)', borderTopColor: '#6ee7ff',
  animation: 'ra-ring 0.9s linear infinite',
};

const dotsRow: React.CSSProperties = { display: 'flex', gap: 6 };

const dot: React.CSSProperties = {
  width: 6, height: 6, borderRadius: '50%', background: '#7c5cff',
  animation: 'ra-dot 1.2s ease-in-out infinite',
};

const msg: React.CSSProperties = {
  fontSize: 13, fontWeight: 600, color: '#e8e8f5', letterSpacing: 0.2,
  animation: 'ra-fade 0.4s ease', minHeight: 18,
};

const sub: React.CSSProperties = { fontSize: 11, color: '#8a8ab0' };
