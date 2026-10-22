import { getActiveLogo, getBrandName } from '../config/branding';
import './LandingPage.css';
import { HERO_COPY } from './landingCopy';

interface LandingPageProps {
  onSignIn: () => void;
  entryMode?: 'sign-in' | 'return';
}

export default function LandingPage({ onSignIn, entryMode = 'sign-in' }: LandingPageProps) {
  const logo = getActiveLogo();
  const brandName = getBrandName();

  return (
    <main className="mah-page">
      <nav className="mah-nav" aria-label="Primary navigation">
        <a className="mah-brand" href="#top" aria-label={`${brandName} home`}>
          <img src={logo.src} alt="" style={{ borderRadius: logo.borderRadius ?? 8 }} />
          <span>{brandName}</span>
        </a>
        <button className="mah-button mah-button--quiet" type="button" onClick={onSignIn}>{entryMode === 'return' ? 'Explore the harness' : 'Sign in'} <ArrowIcon /></button>
      </nav>

      <header className="mah-hero" id="top">
        <div className="mah-orb mah-orb--one" /><div className="mah-orb mah-orb--two" />
        <div className="mah-hero__content">
          <div className="mah-eyebrow"><span />{HERO_COPY.eyebrow}</div>
          <h1>{HERO_COPY.headline} <em>{HERO_COPY.headlineEmphasis}</em></h1>
          <p className="mah-hero__lead">{HERO_COPY.lead}</p>
          <div className="mah-hero__actions">
            <button className="mah-button mah-button--primary" type="button" onClick={onSignIn}>Explore the harness <ArrowIcon /></button>
            <a className="mah-text-link" href="#architecture">See the architecture <DownIcon /></a>
          </div>
          <p className="mah-hero__note">One harness—inside the workspace or any OIDC-connected application.</p>
        </div>
        <OrchestrationVisual />
      </header>

      <section className="mah-architecture" id="architecture" aria-labelledby="architecture-title">
        <div className="mah-architecture__inner">
          <div className="mah-architecture__intro">
            <p className="mah-kicker">How the harness fits</p>
            <h2 id="architecture-title">One media foundation. Every application.</h2>
            <p>Shared identity, agent execution, skills, tools, job state, and artifacts—without rebuilding the stack for each use case.</p>
          </div>
          <figure className="mah-architecture__figure">
            <img src="/media-agent-harness-architecture.png" alt="AWS architecture showing media teams and customer applications entering the Media Agent Harness through CloudFront and OIDC, with AgentCore execution, shared media tools, long-running workflows, S3 media, and durable state." />
            <figcaption><strong>Flagship integration · Unified Media Agent Toolkit micro-workflows</strong><span>Editable architecture draft · AWS services shown at the harness boundary</span></figcaption>
          </figure>
        </div>
      </section>

      <section className="mah-values" id="why" aria-labelledby="values-title">
        <div className="mah-values__inner">
          <div className="mah-values__heading">
            <p className="mah-kicker">Why a media harness</p>
            <h2 id="values-title">Built for media—not generic chat.</h2>
          </div>
          <div className="mah-values__grid">
            <ValueCard icon="⌁" title="Media by reference" text="Large video, audio, and sidecars stay in durable storage—not model context." />
            <ValueCard icon="↻" title="Long-running work" text="Async jobs and parallel DAGs keep durable state beyond a single request." />
            <ValueCard icon="⌘" title="Media-native tools" text="Govern processing, metadata, caption, timeline, and delivery tools through MCP integrations." />
            <ValueCard icon="◆" title="Extensible skills" text="Load, share, and version media expertise without rebuilding agent infrastructure." />
            <ValueCard icon="↗" title="Reviewed improvement" text="Turn evaluation findings into versioned recommendations that teams approve before applying." />
            <ValueCard icon="◫" title="Media-native results" text="Video, timelines, captions, waveforms, documents, and data stay interactive." />
          </div>
          <div className="mah-values__strip" aria-label="Harness delivery options">
            <span><b>01</b> Interactive workspace</span>
            <span><b>02</b> OIDC application integration</span>
            <span><b>03</b> Observable, scoped execution</span>
          </div>
        </div>
      </section>

      <section className="mah-cta">
        <div className="mah-cta__glow" />
        <img src={logo.src} alt="" style={{ borderRadius: logo.borderRadius ?? 12 }} />
        <div><p className="mah-eyebrow">Built on Amazon Bedrock AgentCore</p><h2>Build the media capability once. <em>Extend it everywhere.</em></h2></div>
        <button className="mah-button mah-button--primary" type="button" onClick={onSignIn}>Enter the harness <ArrowIcon /></button>
      </section>

      <footer className="mah-footer">
        <div className="mah-brand"><img src={logo.src} alt="" style={{ borderRadius: logo.borderRadius ?? 8 }} /><span>{brandName}</span></div>
        <p>Media operations · Creative production · Video intelligence</p>
        <p>Powered by AWS and Amazon Bedrock AgentCore</p>
      </footer>
    </main>
  );
}

function OrchestrationVisual() {
  return (
    <div className="mah-visual" aria-hidden="true">
      <div className="mah-visual__label">Live orchestration</div>
      <div className="mah-visual__inputs"><VisualNode icon="▶" label="Source media" /><VisualNode icon="≋" label="Operator intent" /><VisualNode icon="◆" label="Approved skills" /></div>
      <div className="mah-visual__rails"><i /><i /><i /></div>
      <div className="mah-visual__core"><span className="mah-visual__pulse" /><div className="mah-visual__core-logo">MA</div><div><strong>Media Agent Harness</strong><small>Coordinating execution</small></div><span className="mah-visual__live"><i /> Live</span></div>
      <div className="mah-visual__activity">
        <div><span className="mah-activity-icon">✓</span><p><strong>Tool selected</strong><small>Policy checked</small></p></div>
        <div><span className="mah-activity-icon mah-activity-icon--active">↻</span><p><strong>Media processing</strong><small>Job in progress</small></p></div>
        <div><span className="mah-activity-icon">◇</span><p><strong>Artifact ready</strong><small>Output preserved</small></p></div>
      </div>
      <div className="mah-visual__scan" />
    </div>
  );
}


function VisualNode({ icon, label }: { icon: string; label: string }) {
  return <div><span>{icon}</span>{label}</div>;
}

function ValueCard({ icon, title, text }: { icon: string; title: string; text: string }) {
  return <article><span>{icon}</span><div><h3>{title}</h3><p>{text}</p></div></article>;
}

function ArrowIcon() {
  return <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 6l4 4-4 4" /></svg>;
}

function DownIcon() {
  return <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 4v11M6 11l4 4 4-4" /></svg>;
}