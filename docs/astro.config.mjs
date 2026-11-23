// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// Chrome rings the theme select on mouse focus too (:focus-visible matches
// clicks on form controls), so pointer-driven focus is tagged on the label
// here; teaport.css quiets the ring while the flag holds and the outline
// waits for the keyboard.
const themeFocusFlag = `
const themeLabel = document.querySelector('starlight-theme-select label');
const themeSelect = themeLabel?.querySelector('select');
if (themeLabel && themeSelect) {
	const mark = () => themeLabel.setAttribute('data-pointer-focus', '');
	const clear = () => themeLabel.removeAttribute('data-pointer-focus');
	themeLabel.addEventListener('pointerdown', mark);
	themeSelect.addEventListener('blur', clear);
	themeLabel.addEventListener('keydown', clear);
}
`;

// Whole-card click: a card holding a link navigates as one target; the
// cursor comes from teaport.css. Modifier clicks stay with the browser.
const cardClickFlag = `
for (const card of document.querySelectorAll('.card')) {
	const link = card.querySelector('a[href]');
	if (!link) continue;
	card.classList.add('card-link');
	card.addEventListener('click', (e) => {
		if (e.target.closest('a') || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
		link.click();
	});
}
`;

// Heading anchors copy their URL; the icon ticks for a moment to confirm.
// The default jump-to-heading still runs.
const anchorCopyFlag = `
for (const link of document.querySelectorAll('.sl-anchor-link')) {
	const icon = link.querySelector('.sl-anchor-icon');
	const original = icon?.innerHTML;
	link.addEventListener('click', () => {
		const url = new URL(link.getAttribute('href'), location.href).href;
		navigator.clipboard?.writeText(url).catch(() => {});
		if (!icon || !original) return;
		icon.innerHTML =
			'<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z"/></svg>';
		setTimeout(() => {
			icon.innerHTML = original;
		}, 1200);
	});
}
`;

// https://astro.build/config
export default defineConfig({
	site: 'https://docs.teaspoon.tech',
	integrations: [
		starlight({
			title: 'Teaport',
			description: 'Docs for teaport, a local voice assistant for the NVIDIA Jetson Orin.',
			// Styled after the Teaport pages of the marketing site; see the
			// header of src/styles/teaport.css for the files it follows.
			// Fonts are self-hosted (EU GDPR): no requests to Google.
			customCss: ['./src/styles/fonts.css', './src/styles/fonts-teaport.css', './src/styles/teaport.css'],
			head: [
				{
					// Tag pointer-driven focus on the theme select; see the
					// themeFocusFlag note above and teaport.css.
					tag: 'script',
					attrs: { type: 'module' },
					content: themeFocusFlag,
				},
				{
					// Whole-card click; see the cardClickFlag note above.
					tag: 'script',
					attrs: { type: 'module' },
					content: cardClickFlag,
				},
				{
					// Heading anchors copy their URL; see the anchorCopyFlag note.
					tag: 'script',
					attrs: { type: 'module' },
					content: anchorCopyFlag,
				},
			],
			logo: {
				dark: './src/assets/teaport-logo-dark-bg.svg',
				light: './src/assets/teaport-logo-light-bg.svg',
				replacesTitle: true,
			},
			// The header logo links to the teaport page on the marketing site;
			// see src/overrides/SiteTitle.astro.
			components: {
				SiteTitle: './src/overrides/SiteTitle.astro',
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
			sidebar: [
				{
					label: 'Get started',
					items: [
						{ label: 'Overview', slug: 'index' },
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
						{ label: 'Security model', slug: 'reference/security' },
						{ label: 'teaport CLI reference', slug: 'reference/cli' },
						{ label: 'Installer reference', slug: 'reference/installer' },
						{ label: 'Settings reference', slug: 'reference/settings' },
						{ label: 'Open-source licenses', slug: 'reference/licenses' },
					],
				},
			],
		}),
	],
});
