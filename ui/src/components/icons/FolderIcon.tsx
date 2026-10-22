import IconBase, { type IconProps } from './IconBase';

/**
 * A Project. Same open-folder glyph as the sidebar's Projects nav entry, so a
 * project row, the composer chip, and the nav all read as the same thing.
 */
export default function FolderIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M3 7a2 2 0 0 1 2-2h3l2 2h5a2 2 0 0 1 2 2v1" />
      <path d="M3 9h15a2 2 0 0 1 2 2l-1 7a2 2 0 0 1-2 1.7H6a2 2 0 0 1-2-1.7L3 9z" />
    </IconBase>
  );
}
