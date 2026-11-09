// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// https://astro.build/config
export default defineConfig({
	site: 'https://docs.teaspoon.tech',
	integrations: [
		starlight({
			title: 'Teaport',
			description: 'Docs for teaport, a local voice assistant for the NVIDIA Jetson Orin.',
			// Styled after the Teaport pages of the marketing site; see the
			// header of src/styles/teaport.css for the files it follows.
			customCss: ['./src/styles/teaport.css'],
			head: [
				{ tag: 'link', attrs: { rel: 'preconnect', href: 'https://fonts.googleapis.com' } },
				{ tag: 'link', attrs: { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: true } },
				{
					tag: 'link',
					attrs: {
						rel: 'stylesheet',
						href: 'https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400..900;1,9..40,400..900&family=Archivo:wght@700&display=swap',
					},
				},
			],
			logo: {
				dark: './src/assets/teaport-logo-dark-bg.svg',
				light: './src/assets/teaport-logo-light-bg.svg',
				replacesTitle: true,
			},
			// Code sits on the marketing site's terminal ground in both themes.
			expressiveCode: {
				themes: ['github-dark-default'],
				useStarlightUiThemeColors: false,
				styleOverrides: {
					borderRadius: '8px',
					borderColor: 'transparent',
					codeBackground: '#101417',
					codeFontFamily: "ui-monospace, 'SF Mono', 'SFMono-Regular', Menlo, Consolas, monospace",
					uiFontFamily: "'DM Sans', system-ui, sans-serif",
					frames: {
						editorTabBarBackground: '#181e22',
						editorActiveTabBackground: '#101417',
						editorActiveTabIndicatorTopColor: '#82ffb6',
						terminalTitlebarBackground: '#181e22',
						terminalBackground: '#101417',
						terminalTitlebarBorderBottomColor: 'transparent',
						frameBoxShadowCssValue: '0 1px 2px rgba(28,27,24,.05), 0 18px 40px -24px rgba(28,27,24,.28)',
					},
				},
			},
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/Teaspoon-AI/teaport' },
			],
			editLink: {
				baseUrl: 'https://github.com/Teaspoon-AI/teaport/edit/main/docs/',
			},
			sidebar: [
				{
					label: 'Get started',
					items: [
						{ label: 'Prepare the Jetson', slug: 'getting-started/prepare-the-jetson' },
						{ label: 'Install teaport', slug: 'getting-started/installation' },
					],
				},
				{
					label: 'Guides',
					items: [
						{ label: 'Configure teaport', slug: 'guides/configuration' },
						{ label: 'Set up phone calls', slug: 'guides/phone-calls' },
						{ label: 'Set up the Discord bridge', slug: 'guides/discord-bridge' },
						{ label: 'Use a microphone on the device', slug: 'guides/local-audio' },
						{ label: 'Connect the device to Wi-Fi', slug: 'guides/wifi-setup' },
						{ label: 'Update teaport', slug: 'guides/updating' },
						{ label: 'Troubleshooting', slug: 'guides/troubleshooting' },
						{ label: 'FAQ', slug: 'guides/faq' },
					],
				},
				{
					label: 'Reference',
					items: [
						{ label: 'Architecture', slug: 'reference/architecture' },
						{ label: 'teaport CLI reference', slug: 'reference/cli' },
						{ label: 'Installer reference', slug: 'reference/installer' },
						{ label: 'Settings reference', slug: 'reference/settings' },
					],
				},
			],
		}),
	],
});
