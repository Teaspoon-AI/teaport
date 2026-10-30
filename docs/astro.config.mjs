// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightThemeFlexoki from 'starlight-theme-flexoki';

// https://astro.build/config
export default defineConfig({
	site: 'https://docs.teaspoon.tech',
	integrations: [
		starlight({
			title: 'Teaport',
			description: 'Docs for teaport, a local voice assistant for the NVIDIA Jetson Orin.',
			plugins: [starlightThemeFlexoki({ accentColor: 'blue' })],
			logo: {
				dark: './src/assets/teaport-mark-dark-bg.svg',
				light: './src/assets/teaport-mark-light-bg.svg',
			},
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/Teaspoon-AI/teaport' },
			],
			editLink: {
				baseUrl: 'https://github.com/Teaspoon-AI/teaport-docs/edit/main/',
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
