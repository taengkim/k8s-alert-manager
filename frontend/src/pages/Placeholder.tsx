interface PlaceholderProps {
  title: string;
}

export default function Placeholder({ title }: PlaceholderProps) {
  return (
    <div>
      <h2>{title}</h2>
      <p>이 섹션은 아직 준비 중입니다.</p>
    </div>
  );
}
