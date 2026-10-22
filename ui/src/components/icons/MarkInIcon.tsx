import IconBase, { type IconProps } from './IconBase';

/** Set the mark-in at the playhead. */
export default function MarkInIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M5 4v16M19 12H9M13 8l-4 4 4 4" />
    </IconBase>
  );
}
