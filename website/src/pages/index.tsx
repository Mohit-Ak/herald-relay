import React from 'react';
import Layout from '@theme/Layout';

const mono = "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace";
const sans = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

const colors = {
  bg: '#0D0D0D',
  bgCard: '#141414',
  bgCardHover: '#1a1a1a',
  border: '#1f1f1f',
  borderHover: '#2e2e2e',
  accent: '#7C3AED',
  accentLight: '#9D5CF6',
  accentDim: 'rgba(124, 58, 237, 0.15)',
  accentGlow: 'rgba(124, 58, 237, 0.3)',
  text: '#FFFFFF',
  textMuted: '#888888',
  textDim: '#555555',
  green: '#22c55e',
  greenDim: 'rgba(34, 197, 94, 0.15)',
};

const Section: React.FC<{ children: React.ReactNode; style?: React.CSSProperties }> = ({ children, style }) => (
  <section style={{
    maxWidth: 1100,
    margin: '0 auto',
    padding: '96px 24px',
    ...style,
  }}>
    {children}
  </section>
);

const SectionLabel: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div style={{
    display: 'inline-flex',
    alignItems: 'center',
    gap: 8,
    background: colors.accentDim,
    border: `1px solid ${colors.accentGlow}`,
    borderRadius: 100,
    padding: '4px 14px',
    fontSize: 12,
    fontWeight: 600,
    color: colors.accentLight,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    fontFamily: sans,
    marginBottom: 24,
  }}>
    {children}
  </div>
);

const Heading: React.FC<{ children: React.ReactNode; style?: React.CSSProperties }> = ({ children, style }) => (
  <h2 style={{
    fontSize: 'clamp(28px, 4vw, 44px)',
    fontWeight: 700,
    lineHeight: 1.15,
    color: colors.text,
    fontFamily: sans,
    margin: '0 0 16px',
    letterSpacing: '-0.02em',
    ...style,
  }}>
    {children}
  </h2>
);

const SubText: React.FC<{ children: React.ReactNode; style?: React.CSSProperties }> = ({ children, style }) => (
  <p style={{
    fontSize: 18,
    color: colors.textMuted,
    lineHeight: 1.7,
    fontFamily: sans,
    margin: '0 0 40px',
    ...style,
  }}>
    {children}
  </p>
);

const TerminalBlock: React.FC<{ lines: string[] }> = ({ lines }) => (
  <div style={{
    background: '#0a0a0a',
    border: `1px solid ${colors.border}`,
    borderRadius: 12,
    overflow: 'hidden',
    fontFamily: mono,
  }}>
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 6,
      padding: '12px 16px',
      borderBottom: `1px solid ${colors.border}`,
      background: '#111111',
    }}>
      {['#ff5f57', '#febc2e', '#28c840'].map((c, i) => (
        <div key={i} style={{ width: 12, height: 12, borderRadius: '50%', background: c }} />
      ))}
      <span style={{ marginLeft: 8, fontSize: 12, color: colors.textDim, fontFamily: sans }}>terminal</span>
    </div>
    <div style={{ padding: '20px 24px' }}>
      {lines.map((line, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: i < lines.length - 1 ? 10 : 0 }}>
          <span style={{ color: colors.accent, fontSize: 14, userSelect: 'none', marginTop: 1 }}>$</span>
          <span style={{ color: '#e2e2e2', fontSize: 14, lineHeight: 1.6 }}>{line}</span>
        </div>
      ))}
    </div>
  </div>
);

export default function Home(): JSX.Element {
  return (
    <Layout
      title="Talk to your AI agent. From anywhere."
      description="Herald bridges Hermes and your phone. Push notifications, voice bursts, async replies — no server, no port forwarding."
    >
      <div style={{ background: colors.bg, minHeight: '100vh', fontFamily: sans }}>

        {/* ── HERO ── */}
        <div style={{
          position: 'relative',
          overflow: 'hidden',
          borderBottom: `1px solid ${colors.border}`,
        }}>
          {/* radial glow */}
          <div style={{
            position: 'absolute',
            top: '-200px',
            left: '50%',
            transform: 'translateX(-50%)',
            width: 900,
            height: 600,
            background: 'radial-gradient(ellipse, rgba(124,58,237,0.18) 0%, transparent 70%)',
            pointerEvents: 'none',
          }} />
          <Section style={{ padding: '128px 24px 96px', textAlign: 'center' }}>
            <div style={{ marginBottom: 20 }}>
              <SectionLabel>🔔 Now in beta</SectionLabel>
            </div>
            <h1 style={{
              fontSize: 'clamp(40px, 6vw, 80px)',
              fontWeight: 800,
              lineHeight: 1.05,
              letterSpacing: '-0.04em',
              color: colors.text,
              fontFamily: sans,
              margin: '0 0 24px',
            }}>
              Talk to your AI agent.{' '}
              <span style={{
                background: 'linear-gradient(135deg, #7C3AED 0%, #a78bfa 100%)',
                WebkitBackgroundClip: 'text',
                WebkitTextFillColor: 'transparent',
                backgroundClip: 'text',
              }}>
                From anywhere.
              </span>
            </h1>
            <p style={{
              fontSize: 'clamp(16px, 2vw, 21px)',
              color: colors.textMuted,
              maxWidth: 600,
              margin: '0 auto 48px',
              lineHeight: 1.65,
              fontFamily: sans,
            }}>
              Herald connects <strong style={{ color: '#ccc' }}>Hermes AI</strong> to your phone over a secure relay.
              Start a long task, walk away, get a push notification when it's done — then burst-reply by voice in seconds.
            </p>
            <div style={{ display: 'flex', gap: 16, justifyContent: 'center', flexWrap: 'wrap', marginBottom: 64 }}>
              <a
                href="/docs/quickstart"
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 8,
                  background: colors.accent,
                  color: '#fff',
                  padding: '14px 28px',
                  borderRadius: 8,
                  fontWeight: 600,
                  fontSize: 15,
                  textDecoration: 'none',
                  transition: 'background 0.15s',
                  fontFamily: sans,
                }}
              >
                Get started free →
              </a>
              <a
                href="https://github.com/nousresearch/hermes-herald"
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 8,
                  background: 'transparent',
                  color: colors.text,
                  padding: '14px 28px',
                  borderRadius: 8,
                  fontWeight: 600,
                  fontSize: 15,
                  textDecoration: 'none',
                  border: `1px solid ${colors.border}`,
                  fontFamily: sans,
                }}
              >
                ⭐ GitHub
              </a>
            </div>
            <div style={{ maxWidth: 560, margin: '0 auto' }}>
              <TerminalBlock lines={[
                'pip install hermes-herald',
                'hermes config set herald.device_token YOUR_TOKEN',
              ]} />
            </div>
          </Section>
        </div>

        {/* ── HOW IT WORKS ── */}
        <div style={{ borderBottom: `1px solid ${colors.border}` }}>
          <Section>
            <div style={{ textAlign: 'center', marginBottom: 64 }}>
              <SectionLabel>How it works</SectionLabel>
              <Heading>Three steps to async AI</Heading>
              <SubText style={{ maxWidth: 520, margin: '0 auto 0' }}>
                No server to manage. No port forwarding. No always-on voice stream burning your API budget.
              </SubText>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 24 }}>
              {[
                {
                  step: '01',
                  icon: '🔌',
                  title: 'Install the plugin',
                  body: 'Run pip install hermes-herald and drop two lines into your Hermes config. The plugin connects outbound to the Herald relay — no inbound firewall rules, no VPN.',
                },
                {
                  step: '02',
                  icon: '📱',
                  title: 'Scan your QR code',
                  body: 'Open the Herald mobile app, tap "Link Device", and scan the QR shown by hermes herald qr. Your phone is now a registered push target — tokens stay on your device.',
                },
                {
                  step: '03',
                  icon: '🔔',
                  title: 'Get notified & reply',
                  body: 'Kick off a long-running Hermes task and put your laptop away. When the agent needs you — or finishes — you get a rich push notification. Tap to reply by voice or text in under 5 seconds.',
                },
              ].map((item) => (
                <div key={item.step} style={{
                  background: colors.bgCard,
                  border: `1px solid ${colors.border}`,
                  borderRadius: 16,
                  padding: 32,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
                    <span style={{
                      fontSize: 11,
                      fontWeight: 700,
                      color: colors.accent,
                      fontFamily: mono,
                      letterSpacing: '0.1em',
                    }}>
                      STEP {item.step}
                    </span>
                    <span style={{ fontSize: 22 }}>{item.icon}</span>
                  </div>
                  <h3 style={{
                    fontSize: 20,
                    fontWeight: 700,
                    color: colors.text,
                    margin: '0 0 12px',
                    fontFamily: sans,
                    letterSpacing: '-0.02em',
                  }}>
                    {item.title}
                  </h3>
                  <p style={{ fontSize: 15, color: colors.textMuted, lineHeight: 1.65, margin: 0, fontFamily: sans }}>
                    {item.body}
                  </p>
                </div>
              ))}
            </div>
          </Section>
        </div>

        {/* ── BURST MODEL ── */}
        <div style={{ borderBottom: `1px solid ${colors.border}` }}>
          <Section>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 64, alignItems: 'center' }}>
              <div>
                <SectionLabel>The burst model</SectionLabel>
                <Heading>Pay for conversations,<br />not idle silence</Heading>
                <p style={{ fontSize: 16, color: colors.textMuted, lineHeight: 1.75, marginBottom: 20, fontFamily: sans }}>
                  Traditional voice assistants hold an open audio stream the entire time you're "available." That means constant transcription costs, even when nobody's talking.
                </p>
                <p style={{ fontSize: 16, color: colors.textMuted, lineHeight: 1.75, marginBottom: 0, fontFamily: sans }}>
                  Herald uses a <strong style={{ color: '#ccc' }}>push-notified burst model</strong>: the audio channel only opens when you tap reply. A typical back-and-forth with your agent costs a few cents — not dollars per hour.
                </p>
              </div>
              <div style={{
                background: '#0a0a0a',
                border: `1px solid ${colors.border}`,
                borderRadius: 16,
                padding: 32,
                fontFamily: mono,
                fontSize: 13,
                lineHeight: 2,
              }}>
                <div style={{ color: colors.textDim, marginBottom: 8, fontSize: 11, letterSpacing: '0.1em' }}>BURST FLOW</div>
                {[
                  { label: 'Hermes finishes task', color: colors.accentLight, arrow: false },
                  { label: '      ↓', color: colors.textDim, arrow: true },
                  { label: 'Relay sends push notification', color: '#60a5fa', arrow: false },
                  { label: '      ↓', color: colors.textDim, arrow: true },
                  { label: 'You tap reply on phone', color: colors.green, arrow: false },
                  { label: '      ↓', color: colors.textDim, arrow: true },
                  { label: '🎙  Audio burst opens  (~8s)', color: colors.text, arrow: false },
                  { label: '      ↓', color: colors.textDim, arrow: true },
                  { label: 'Transcribe → Agent → TTS', color: colors.accentLight, arrow: false },
                  { label: '      ↓', color: colors.textDim, arrow: true },
                  { label: '✅ Channel closes. Cost: ~$0.05', color: colors.green, arrow: false },
                ].map((row, i) => (
                  <div key={i} style={{ color: row.color }}>{row.label}</div>
                ))}
              </div>
            </div>
          </Section>
        </div>

        {/* ── PRICING ── */}
        <div style={{ borderBottom: `1px solid ${colors.border}` }}>
          <Section>
            <div style={{ textAlign: 'center', marginBottom: 56 }}>
              <SectionLabel>Pricing</SectionLabel>
              <Heading>Simple, transparent costs</Heading>
              <SubText style={{ maxWidth: 480, margin: '0 auto 0' }}>
                Both plans include a free tier of 20 bursts per month. No credit card required to start.
              </SubText>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, maxWidth: 760, margin: '0 auto' }}>
              {[
                {
                  plan: 'BYOK',
                  subtitle: 'Bring Your Own Key',
                  highlight: false,
                  features: [
                    { label: 'Gemini API', value: 'Your key' },
                    { label: 'Relay fee', value: '$2 / month' },
                    { label: 'Per burst', value: 'API cost only' },
                    { label: 'Free tier', value: '20 bursts' },
                    { label: 'Best for', value: 'Developers' },
                  ],
                  cta: 'Get your API key →',
                  ctaHref: 'https://aistudio.google.com/app/apikey',
                },
                {
                  plan: 'Credits',
                  subtitle: 'Managed by Herald',
                  highlight: true,
                  features: [
                    { label: 'Gemini API', value: 'We provide' },
                    { label: 'Relay fee', value: 'Included' },
                    { label: 'Per burst', value: '~$0.05' },
                    { label: 'Free tier', value: '20 bursts' },
                    { label: 'Best for', value: 'Everyone' },
                  ],
                  cta: 'Start free →',
                  ctaHref: '/docs/quickstart',
                },
              ].map((plan) => (
                <div key={plan.plan} style={{
                  background: plan.highlight ? 'linear-gradient(145deg, #1a0f2e, #120c20)' : colors.bgCard,
                  border: `1px solid ${plan.highlight ? colors.accentGlow : colors.border}`,
                  borderRadius: 16,
                  padding: 32,
                  display: 'flex',
                  flexDirection: 'column',
                }}>
                  <div style={{ marginBottom: 24 }}>
                    <div style={{ fontSize: 22, fontWeight: 800, color: colors.text, fontFamily: sans, letterSpacing: '-0.02em' }}>
                      {plan.plan}
                    </div>
                    <div style={{ fontSize: 14, color: colors.textMuted, fontFamily: sans, marginTop: 4 }}>
                      {plan.subtitle}
                    </div>
                  </div>
                  <div style={{ flex: 1 }}>
                    {plan.features.map((f) => (
                      <div key={f.label} style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        padding: '10px 0',
                        borderBottom: `1px solid ${colors.border}`,
                        fontFamily: sans,
                      }}>
                        <span style={{ fontSize: 14, color: colors.textMuted }}>{f.label}</span>
                        <span style={{ fontSize: 14, fontWeight: 600, color: colors.text }}>{f.value}</span>
                      </div>
                    ))}
                  </div>
                  <a
                    href={plan.ctaHref}
                    style={{
                      display: 'block',
                      textAlign: 'center',
                      marginTop: 28,
                      padding: '12px 20px',
                      borderRadius: 8,
                      background: plan.highlight ? colors.accent : 'transparent',
                      border: `1px solid ${plan.highlight ? colors.accent : colors.border}`,
                      color: colors.text,
                      fontWeight: 600,
                      fontSize: 14,
                      textDecoration: 'none',
                      fontFamily: sans,
                    }}
                  >
                    {plan.cta}
                  </a>
                </div>
              ))}
            </div>
          </Section>
        </div>

        {/* ── INSTALL ── */}
        <div style={{ borderBottom: `1px solid ${colors.border}` }}>
          <Section>
            <div style={{ textAlign: 'center', marginBottom: 56 }}>
              <SectionLabel>Install</SectionLabel>
              <Heading>Up and running in 2 minutes</Heading>
            </div>
            <div style={{ maxWidth: 680, margin: '0 auto' }}>
              {[
                {
                  n: 1,
                  title: 'Install the plugin',
                  code: ['pip install hermes-herald'],
                },
                {
                  n: 2,
                  title: 'Add to your Hermes config (~/.hermes/config.yaml)',
                  code: [
                    'plugins:',
                    '  herald-relay:',
                    '    enabled: true',
                    '    device_token: YOUR_TOKEN   # from Herald app',
                    '    mode: credits              # or byok',
                    '    # gemini_api_key: AIza...  # if using byok mode',
                  ],
                },
                {
                  n: 3,
                  title: 'Verify the connection',
                  code: ['hermes plugins list'],
                },
                {
                  n: 4,
                  title: 'Start Hermes and go AFK',
                  code: [
                    'hermes "Research the top 5 open-source LLM inference engines and write me a comparison doc"',
                    '# Herald will notify you when done',
                  ],
                },
              ].map((step) => (
                <div key={step.n} style={{ marginBottom: 32 }}>
                  <div style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 12,
                    marginBottom: 12,
                  }}>
                    <div style={{
                      width: 28,
                      height: 28,
                      borderRadius: '50%',
                      background: colors.accentDim,
                      border: `1px solid ${colors.accentGlow}`,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      fontSize: 12,
                      fontWeight: 700,
                      color: colors.accentLight,
                      fontFamily: mono,
                      flexShrink: 0,
                    }}>
                      {step.n}
                    </div>
                    <span style={{ fontSize: 15, fontWeight: 600, color: colors.text, fontFamily: sans }}>
                      {step.title}
                    </span>
                  </div>
                  <TerminalBlock lines={step.code} />
                </div>
              ))}
            </div>
          </Section>
        </div>

        {/* ── FOOTER ── */}
        <footer style={{
          borderTop: `1px solid ${colors.border}`,
          padding: '40px 24px',
          textAlign: 'center',
          fontFamily: sans,
        }}>
          <div style={{ maxWidth: 1100, margin: '0 auto', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ fontSize: 20 }}>🔔</span>
              <span style={{ fontWeight: 700, color: colors.text, fontSize: 15 }}>Herald</span>
              <span style={{ color: colors.textDim, fontSize: 13 }}>by Nous Research</span>
            </div>
            <div style={{ fontSize: 13, color: colors.textDim }}>
              © {new Date().getFullYear()} Nous Research. All rights reserved.
            </div>
            <div style={{ display: 'flex', gap: 24 }}>
              {[
                { label: 'Docs', href: '/docs/quickstart' },
                { label: 'GitHub', href: 'https://github.com/nousresearch/hermes-herald' },
                { label: 'Discord', href: 'https://discord.gg/nousresearch' },
              ].map((link) => (
                <a
                  key={link.label}
                  href={link.href}
                  style={{ fontSize: 14, color: colors.textMuted, textDecoration: 'none', fontFamily: sans }}
                >
                  {link.label}
                </a>
              ))}
            </div>
          </div>
        </footer>
      </div>
    </Layout>
  );
}
