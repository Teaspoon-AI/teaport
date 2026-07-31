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
					label: 'Getting Started',
					items: [{ label: 'Installation', slug: 'getting-started/installation' }],
				},
				{
					label: 'Guides',
					items: [
						{ label: 'Configuration', slug: 'guides/configuration' },
						{ label: 'Troubleshooting', slug: 'guides/troubleshooting' },
						{ label: 'FAQ', slug: 'guides/faq' },
					],
				},
				{
					label: 'Reference',
					items: [{ label: 'Architecture', slug: 'reference/architecture' }],
				},
			],
		}),
	],
});
