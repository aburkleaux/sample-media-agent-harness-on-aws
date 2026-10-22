import React, { useState, useEffect } from 'react';
import { theme } from '../../styles/tokens';
import { THEMES, readThemePreference, writeThemePreference, type ThemeName } from '../../styles/themePreference';

type FontSize = 'small' | 'medium' | 'large';

export default function GeneralSettings() {
  const [themeName, setThemeName] = useState<ThemeName>(readThemePreference);
  const [fontSize, setFontSize] = useState<FontSize>(() => (localStorage.getItem('lab-font-size') as FontSize) || 'medium');

  useEffect(() => {
    writeThemePreference(themeName);
  }, [themeName]);

  useEffect(() => {
    localStorage.setItem('lab-font-size', fontSize);
    document.documentElement.setAttribute('data-font-size', fontSize);
  }, [fontSize]);

  return (
    <div>
      <h2 style={{ margin: '0 0 4px', fontSize: 'var(--fs-lg)', fontWeight: 600, color: theme.colors.textPrimary }}>General</h2>
      <p style={{ margin: '0 0 16px', fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary }}>
        Appearance &amp; workspace defaults.
      </p>

      {/* Appearance Section */}
      <div style={sectionStyle}>
        <h3 style={sectionTitleStyle}>Appearance</h3>

        {/*
          A picker rather than a switch: there are three themes now, and a
          boolean cannot express that. Same segmented control as Font Size below,
          so the two appearance settings behave identically.
        */}
        <div style={settingRowStyle}>
          <div style={{ flex: 1 }}>
            <div style={settingLabelStyle}>Theme</div>
            <div style={settingDescStyle}>Light, dark, or the near-black KiroCrew palette</div>
          </div>
          <div style={fontSizeGroupStyle} role="group" aria-label="Theme">
            {THEMES.map(option => (
              <button
                key={option.id}
                onClick={() => setThemeName(option.id)}
                style={themeName === option.id ? fontSizeBtnActiveStyle : fontSizeBtnStyle}
                aria-pressed={themeName === option.id}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        {/* Font Size */}
        <div style={settingRowStyle}>
          <div style={{ flex: 1 }}>
            <div style={settingLabelStyle}>Font Size</div>
            <div style={settingDescStyle}>Adjust text size across the interface</div>
          </div>
          <div style={fontSizeGroupStyle}>
            {(['small', 'medium', 'large'] as FontSize[]).map(size => (
              <button
                key={size}
                onClick={() => setFontSize(size)}
                style={fontSize === size ? fontSizeBtnActiveStyle : fontSizeBtnStyle}
              >
                <span style={{ fontSize: size === 'small' ? 11 : size === 'medium' ? 13 : 15 }}>A</span>
                <span style={{ fontSize: 'var(--fs-3xs)', marginTop: 1 }}>{size.charAt(0).toUpperCase() + size.slice(1)}</span>
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Preview */}
      <div style={previewSectionStyle}>
        <h3 style={sectionTitleStyle}>Preview</h3>
        <div style={previewBoxStyle}>
          <p style={{ margin: 0, fontSize: fontSize === 'small' ? 12 : fontSize === 'medium' ? 14 : 17 }}>
            This is a preview of how text will appear with your current font size setting.
          </p>
        </div>
      </div>
    </div>
  );
}

// --- Styles ---

const sectionStyle: React.CSSProperties = {
  marginBottom: 28,
};

const sectionTitleStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)', fontWeight: 600, color: theme.colors.textPrimary,
  margin: '0 0 14px', paddingBottom: 8, borderBottom: `1px solid ${theme.colors.border}`,
};

const settingRowStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
  padding: '11px 0', borderBottom: `1px solid ${theme.colors.border}`,
};

const settingLabelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)', fontWeight: 500, color: theme.colors.textPrimary,
};

const settingDescStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, marginTop: 2,
};

const fontSizeGroupStyle: React.CSSProperties = {
  display: 'flex', gap: 4, borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`, overflow: 'hidden',
};

const fontSizeBtnStyle: React.CSSProperties = {
  display: 'flex', flexDirection: 'column', alignItems: 'center',
  padding: '6px 12px', border: 'none', background: theme.colors.surface,
  cursor: 'pointer', color: theme.colors.textSecondary, gap: 1, minWidth: 52,
};

const fontSizeBtnActiveStyle: React.CSSProperties = {
  ...fontSizeBtnStyle,
  background: theme.colors.activeBg, color: theme.colors.primary, fontWeight: 600,
};

const previewSectionStyle: React.CSSProperties = {
  marginBottom: 28,
};

const previewBoxStyle: React.CSSProperties = {
  padding: '16px', borderRadius: theme.radius.panel,
  border: `1px solid ${theme.colors.border}`, background: theme.colors.background,
};
