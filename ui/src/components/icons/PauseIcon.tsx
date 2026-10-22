import IconBase, { type IconProps } from './IconBase';

/** Pause / stop. */
export default function PauseIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M8 5v14M16 5v14" />
    </IconBase>
  );
}
