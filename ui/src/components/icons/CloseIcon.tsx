import IconBase, { type IconProps } from './IconBase';

/** Dismiss / remove. */
export default function CloseIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M6 6l12 12M18 6L6 18" />
    </IconBase>
  );
}
