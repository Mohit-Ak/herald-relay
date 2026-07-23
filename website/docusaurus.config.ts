import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Herald',
  tagline: 'Talk to your Hermes agent, anywhere.',
  favicon: 'img/favicon.ico',

  url: 'https://herald.app',
  baseUrl: '/',

  organizationName: 'Mohit-Ak',
  projectName: 'herald-relay',

  onBrokenLinks: 'throw',
  onBrokenMarkdownLinks: 'warn',
  onBrokenAnchors: 'ignore',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/Mohit-Ak/herald-relay/tree/main/website/',
        },
        blog: false,
        theme: {
          customCss: undefined,
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      defaultMode: 'dark',
      disableSwitch: true,
      respectPrefersColorScheme: false,
    },
    image: 'img/herald-social-card.png',
    navbar: {
      title: 'Herald',
      logo: {
        alt: 'Herald Logo',
        src: 'img/logo.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docsSidebar',
          position: 'left',
          label: 'Docs',
        },
        {
          to: '/#pricing',
          label: 'Pricing',
          position: 'left',
        },
        {
          href: 'https://github.com/Mohit-Ak/herald-relay',
          label: 'GitHub',
          position: 'right',
        },
        {
          href: 'https://herald.app/download',
          label: 'Download App',
          position: 'right',
          className: 'navbar-download-btn',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Product',
          items: [
            { label: 'Download App', href: 'https://herald.app/download' },
            { label: 'Pricing', to: '/#pricing' },
            { label: 'Quickstart', to: '/docs/quickstart' },
          ],
        },
        {
          title: 'Docs',
          items: [
            { label: 'How It Works', to: '/docs/how-it-works' },
            { label: 'Plugin Config', to: '/docs/plugin-config' },
            { label: 'Billing', to: '/docs/billing' },
          ],
        },
        {
          title: 'Community',
          items: [
            { label: 'GitHub', href: 'https://github.com/Mohit-Ak/herald-relay' },
            { label: 'Discord', href: 'https://discord.gg/herald' },
            { label: 'Twitter / X', href: 'https://twitter.com/herald_app' },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Herald. Built on Hermes.`,
    },
    prism: {
      theme: {
        plain: { color: '#D4D4D4', backgroundColor: '#1E1E1E' },
        styles: [],
      },
      darkTheme: {
        plain: { color: '#D4D4D4', backgroundColor: '#1E1E1E' },
        styles: [],
      },
      additionalLanguages: ['bash', 'yaml', 'python'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
