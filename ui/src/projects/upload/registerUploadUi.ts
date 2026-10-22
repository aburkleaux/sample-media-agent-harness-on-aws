/**
 * Fills the chat composer's Upload_Sheet slot with the chat upload sheet. Imported once for its side effect by
 * `main.tsx`, so the composer (and its tests) depend only on the slot, not on the sheet.
 */
import { registerUploadSheet } from '../../components/chat/composerSlots';
import ChatUploadSheet from '../../components/chat/ChatUploadSheet';

registerUploadSheet(ChatUploadSheet);
