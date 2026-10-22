import IconBase, { type IconProps } from './IconBase';

/** Set the mark-out at the playhead. */
export default function MarkOutIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M19 4v16M5 12h10M11 8l4 4-4 4" />
    </IconBase>
  );
}
