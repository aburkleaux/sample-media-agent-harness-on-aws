/**
 * Fills the + menu's Web_Search_Toggle slot. Imported once for its side effect by `main.tsx`, like
 * `upload/registerUploadUi.ts`, so the Plus_Menu (and its tests) depend only on the slot. The toggle itself shows
 * disabled where `/config.webSearchAvailable` is false.
 */
import { registerWebSearchToggleSlot } from '../components/chat/composerSlots';
import WebSearchToggle from '../components/chat/WebSearchToggle';

registerWebSearchToggleSlot(WebSearchToggle);
