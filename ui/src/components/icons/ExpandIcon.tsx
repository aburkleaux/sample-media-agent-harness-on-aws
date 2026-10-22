import IconBase, { type IconProps } from './IconBase';

/** Expand / open in its own tab: two diagonal arrows. */
export default function ExpandIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7" />
    </IconBase>
  );
}
