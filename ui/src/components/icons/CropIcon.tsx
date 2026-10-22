import IconBase, { type IconProps } from './IconBase';

/** Crop to a region. */
export default function CropIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M6 2v14a2 2 0 0 0 2 2h14M2 6h14a2 2 0 0 1 2 2v14" />
    </IconBase>
  );
}
